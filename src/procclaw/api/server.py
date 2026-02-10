"""FastAPI server for ProcClaw API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

if TYPE_CHECKING:
    from procclaw.core.supervisor import Supervisor

# Static files directory
STATIC_DIR = Path(__file__).parent.parent / "web" / "static"

# Global supervisor reference (set by daemon)
_supervisor: "Supervisor | None" = None


def set_supervisor(supervisor: "Supervisor") -> None:
    """Set the global supervisor reference."""
    global _supervisor
    _supervisor = supervisor


def get_supervisor() -> "Supervisor":
    """Get the supervisor instance."""
    if _supervisor is None:
        raise HTTPException(status_code=503, detail="Supervisor not initialized")
    return _supervisor


# Security
security = HTTPBearer(auto_error=False)


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> bool:
    """Verify the API token if authentication is enabled.
    
    Skips auth for:
    - When auth is disabled in config
    - Requests from web UI (same origin via Referer header)
    
    Requires token for:
    - External API calls when auth is enabled
    """
    if _supervisor is None:
        return True

    config = _supervisor.config
    if not config.api.auth.enabled:
        return True

    # Skip auth for same-origin requests (web UI)
    referer = request.headers.get("referer", "")
    host = request.headers.get("host", "")
    origin = request.headers.get("origin", "")
    
    # If referer/origin matches our host, it's from the web UI
    if host and (referer.startswith(f"http://{host}") or 
                 referer.startswith(f"https://{host}") or
                 origin == f"http://{host}" or
                 origin == f"https://{host}"):
        return True
    
    # Also allow if X-Requested-From header is set to "web-ui"
    if request.headers.get("x-requested-from") == "web-ui":
        return True

    # External request - require token
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authorization token")

    expected_token = config.api.auth.token
    if credentials.credentials != expected_token:
        raise HTTPException(status_code=401, detail="Invalid token")

    return True


# Response Models
class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float | None = None
    jobs_running: int = 0
    jobs_total: int = 0


class JobSummary(BaseModel):
    id: str
    name: str
    type: str
    status: str
    enabled: bool
    paused: bool = False
    pid: int | None = None
    started_at: str | None = None
    uptime_seconds: float | None = None
    next_run: str | None = None
    restart_count: int = 0
    tags: list[str] = []
    cmd: str | None = None
    cwd: str | None = None
    description: str | None = None
    schedule: str | None = None
    run_at: str | None = None


class JobDetail(JobSummary):
    description: str = ""
    last_exit_code: int | None = None
    last_error: str | None = None
    cpu_percent: float | None = None
    memory_mb: float | None = None
    last_run: dict | None = None
    # Additional fields for detail view
    cmd: str | None = None
    cwd: str | None = None
    schedule: str | None = None
    run_at: str | None = None  # ISO datetime for oneshot jobs
    max_retries: int | None = None
    timeout_seconds: int | None = None


class JobListResponse(BaseModel):
    jobs: list[JobSummary]
    total: int


class HealingSummary(BaseModel):
    """Summary of self-healing for a run."""
    status: str | None = None  # in_progress, fixed, gave_up, awaiting_approval
    attempts: int = 0
    has_session: bool = False
    root_cause: str | None = None
    fix_summary: str | None = None


class RunSummary(BaseModel):
    id: int
    job_id: str
    job_name: str | None = None
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    trigger: str
    status: str  # running, success, failed, healed
    error: str | None = None
    cmd: str | None = None
    composite_id: str | None = None  # workflow ID if part of chain/group/chord
    session_key: str | None = None  # OpenClaw session key
    has_transcript: bool = False  # Whether session transcript is available
    healing: HealingSummary | None = None  # Self-healing info if applicable
    original_exit_code: int | None = None  # Exit code before healing fixed it


class RunListResponse(BaseModel):
    runs: list[RunSummary]
    total: int


class ActionResponse(BaseModel):
    success: bool
    message: str
    job_id: str
    pid: int | None = None


class LogsResponse(BaseModel):
    job_id: str
    lines: list[str]
    total_lines: int
    source: str = "file"  # file, sqlite, none


class MetricsResponse(BaseModel):
    text: str


# Request Models (for composite jobs)
class ChainRequest(BaseModel):
    job_ids: list[str]
    composite_id: str | None = None


class GroupRequest(BaseModel):
    job_ids: list[str]
    composite_id: str | None = None


class ChordRequest(BaseModel):
    job_ids: list[str]
    callback: str
    composite_id: str | None = None


# Create FastAPI app
def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(
        title="ProcClaw API",
        description="Process Manager for OpenClaw",
        version="0.1.0",
    )

    # Health endpoint
    @app.get("/health", response_model=HealthResponse)
    async def health():
        """Get daemon health status."""
        from procclaw import __version__

        supervisor = get_supervisor()

        # Calculate uptime
        uptime = None
        # TODO: Track daemon start time

        running_jobs = sum(1 for s in supervisor.list_jobs() if s["status"] == "running")
        total_jobs = len(supervisor.jobs.jobs)

        return HealthResponse(
            status="healthy",
            version=__version__,
            uptime_seconds=uptime,
            jobs_running=running_jobs,
            jobs_total=total_jobs,
        )

    # Jobs endpoints
    @app.get("/api/v1/jobs", response_model=JobListResponse)
    async def list_jobs(
        status: str | None = Query(None, description="Filter by status"),
        tag: str | None = Query(None, description="Filter by tag"),
        tags: str | None = Query(None, description="Filter by multiple tags (comma-separated)"),
        type: str | None = Query(None, description="Filter by type (scheduled/continuous/manual)"),
        q: str | None = Query(None, description="Search in name and description"),
        enabled: bool | None = Query(None, description="Filter by enabled status"),
        _auth: bool = Depends(verify_token),
    ):
        """List all jobs with filtering and search."""
        supervisor = get_supervisor()
        jobs = supervisor.list_jobs()

        # Apply filters
        if status:
            jobs = [j for j in jobs if j["status"] == status]
        if tag:
            jobs = [j for j in jobs if tag in j.get("tags", [])]
        if tags:
            tag_list = [t.strip() for t in tags.split(",")]
            jobs = [j for j in jobs if any(t in j.get("tags", []) for t in tag_list)]
        if type:
            jobs = [j for j in jobs if j["type"] == type]
        if enabled is not None:
            jobs = [j for j in jobs if j["enabled"] == enabled]
        if q:
            q_lower = q.lower()
            jobs = [j for j in jobs if (
                q_lower in j["name"].lower() or 
                q_lower in j.get("description", "").lower() or
                q_lower in j["id"].lower() or
                any(q_lower in t.lower() for t in j.get("tags", []))
            )]

        return JobListResponse(
            jobs=[
                JobSummary(
                    id=j["id"],
                    name=j["name"],
                    type=j["type"],
                    status=j["status"],
                    enabled=j["enabled"],
                    paused=j.get("paused", False),
                    pid=j.get("pid"),
                    started_at=j.get("started_at"),
                    uptime_seconds=j.get("uptime_seconds"),
                    next_run=j.get("next_run"),
                    restart_count=j.get("restart_count", 0),
                    tags=j.get("tags", []),
                    cmd=j.get("cmd"),
                    cwd=j.get("cwd"),
                    description=j.get("description"),
                    schedule=j.get("schedule"),
                    run_at=j.get("run_at"),
                )
                for j in jobs
            ],
            total=len(jobs),
        )

    @app.post("/api/v1/jobs", response_model=ActionResponse)
    async def create_job(
        job_data: dict,
        _auth: bool = Depends(verify_token),
    ):
        """Create a new job by adding to jobs.yaml."""
        import yaml
        from procclaw.config import DEFAULT_JOBS_FILE
        from datetime import datetime
        
        supervisor = get_supervisor()
        
        # Validate required fields
        job_id = job_data.get("id")
        if not job_id:
            raise HTTPException(status_code=400, detail="Job ID is required")
        if not job_data.get("cmd"):
            raise HTTPException(status_code=400, detail="Command is required")
        
        # Check if job already exists
        if supervisor.jobs.get_job(job_id):
            raise HTTPException(status_code=409, detail=f"Job '{job_id}' already exists")
        
        # Build job config
        job_config = {
            "name": job_data.get("name") or job_id,
            "cmd": job_data.get("cmd"),
            "type": job_data.get("type", "manual"),
            "enabled": job_data.get("enabled", True),
        }
        
        # Optional fields
        if job_data.get("cwd"):
            job_config["cwd"] = job_data["cwd"]
        if job_data.get("description"):
            job_config["description"] = job_data["description"]
        if job_data.get("schedule"):
            job_config["schedule"] = job_data["schedule"]
        if job_data.get("run_at"):
            job_config["run_at"] = job_data["run_at"]
        if job_data.get("tags"):
            if isinstance(job_data["tags"], str):
                job_config["tags"] = [t.strip() for t in job_data["tags"].split(",") if t.strip()]
            else:
                job_config["tags"] = job_data["tags"]
        if job_data.get("env"):
            # Parse env vars from string format KEY=value
            if isinstance(job_data["env"], str):
                env_dict = {}
                for line in job_data["env"].strip().split("\n"):
                    if "=" in line:
                        key, value = line.split("=", 1)
                        env_dict[key.strip()] = value.strip()
                if env_dict:
                    job_config["env"] = env_dict
            else:
                job_config["env"] = job_data["env"]
        
        # Add metadata
        job_config["_metadata"] = {
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        
        # Read current config
        try:
            with open(DEFAULT_JOBS_FILE, "r") as f:
                config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            config = {}
        
        if "jobs" not in config:
            config["jobs"] = {}
        
        # Add new job
        config["jobs"][job_id] = job_config
        
        # Write back
        with open(DEFAULT_JOBS_FILE, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        
        # Reload config
        supervisor.reload_jobs()
        
        return ActionResponse(
            success=True,
            message=f"Job '{job_id}' created successfully",
            job_id=job_id,
        )

    @app.get("/api/v1/runs", response_model=RunListResponse)
    async def list_runs(
        job_id: str | None = Query(None, description="Filter by job ID"),
        status: str | None = Query(None, description="Filter by status (success/failed/running)"),
        trigger: str | None = Query(None, description="Filter by trigger type"),
        limit: int = Query(100, le=500, description="Max runs to return"),
        _auth: bool = Depends(verify_token),
    ):
        """List job execution history."""
        supervisor = get_supervisor()
        
        # Get runs from database
        runs = supervisor.db.get_runs(job_id=job_id, limit=limit)
        
        # Enrich with job info and compute status
        result = []
        for run in runs:
            job = supervisor.jobs.get_job(run.job_id)
            
            # Determine status
            if run.finished_at is None:
                run_status = "running"
            elif run.exit_code == 0:
                run_status = "success"
            else:
                run_status = "failed"
            
            # Filter by status if specified
            if status and run_status != status:
                continue
            
            # Filter by trigger if specified
            if trigger and run.trigger != trigger:
                continue
            
            # Build healing summary if applicable
            healing_summary = None
            if run.healing_status:
                healing_result = run.healing_result or {}
                analysis = healing_result.get("analysis", {})
                healing_summary = HealingSummary(
                    status=run.healing_status,
                    attempts=run.healing_attempts,
                    has_session=bool(run.healing_session_key),
                    root_cause=analysis.get("root_cause"),
                    fix_summary=healing_result.get("summary"),
                )
                # Update run status to "healed" if healing was successful
                if run.healing_status == "fixed":
                    run_status = "healed"
            
            result.append(RunSummary(
                id=run.id,
                job_id=run.job_id,
                job_name=job.name if job else None,
                started_at=run.started_at.isoformat(),
                finished_at=run.finished_at.isoformat() if run.finished_at else None,
                exit_code=run.exit_code,
                duration_seconds=run.duration_seconds,
                trigger=run.trigger,
                status=run_status,
                error=run.error,
                cmd=job.cmd if job else None,
                composite_id=run.composite_id,
                session_key=run.session_key,
                has_transcript=bool(run.session_transcript),
                healing=healing_summary,
                original_exit_code=run.original_exit_code,
            ))
        
        return RunListResponse(runs=result, total=len(result))

    @app.get("/api/v1/runs/{run_id}/logs")
    async def get_run_logs(
        run_id: int,
        level: str | None = Query(None, description="Filter by level (stdout/stderr)"),
        limit: int = Query(5000, le=10000, description="Max lines to return"),
        _auth: bool = Depends(verify_token),
    ):
        """Get logs for a specific job run from SQLite."""
        supervisor = get_supervisor()
        
        # Get logs from database
        logs = supervisor.db.get_logs(run_id=run_id, level=level, limit=limit)
        
        if not logs:
            # Fallback: check if run exists
            runs = supervisor.db.get_runs(limit=500)
            run = next((r for r in runs if r.id == run_id), None)
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
            # Run exists but no logs in DB yet
            return {"run_id": run_id, "lines": [], "total": 0, "message": "No logs stored for this run"}
        
        return {
            "run_id": run_id,
            "lines": [log["line"] for log in logs],
            "total": len(logs),
        }

    @app.get("/api/v1/runs/{run_id}/session")
    async def get_run_session(
        run_id: int,
        _auth: bool = Depends(verify_token),
    ):
        """Get OpenClaw session transcript for a job run."""
        import json
        from pathlib import Path
        
        supervisor = get_supervisor()
        
        # Find the run
        runs = supervisor.db.get_runs(limit=1000)
        run = next((r for r in runs if r.id == run_id), None)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        
        if not run.session_transcript:
            raise HTTPException(status_code=404, detail="No session transcript available for this run")
        
        transcript_path = Path(run.session_transcript)
        if not transcript_path.exists():
            raise HTTPException(status_code=404, detail=f"Transcript file not found: {run.session_transcript}")
        
        # Read and parse the JSONL transcript
        messages = []
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        msg = json.loads(line)
                        messages.append(msg)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error reading transcript: {e}")
        
        return {
            "run_id": run_id,
            "job_id": run.job_id,
            "session_key": run.session_key,
            "transcript_path": str(transcript_path),
            "messages": messages,
            "message_count": len(messages),
        }

    @app.get("/api/v1/runs/{run_id}/healing")
    async def get_run_healing(
        run_id: int,
        _auth: bool = Depends(verify_token),
    ):
        """Get detailed self-healing info for a job run."""
        supervisor = get_supervisor()
        
        # Find the run
        runs = supervisor.db.get_runs(limit=1000)
        run = next((r for r in runs if r.id == run_id), None)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        
        if not run.healing_status:
            raise HTTPException(status_code=404, detail="No healing info available for this run")
        
        return {
            "run_id": run_id,
            "job_id": run.job_id,
            "healing_status": run.healing_status,
            "healing_attempts": run.healing_attempts,
            "healing_session_key": run.healing_session_key,
            "healing_result": run.healing_result,
            "original_exit_code": run.original_exit_code,
            "final_exit_code": run.exit_code,
        }

    @app.post("/api/v1/jobs/{job_id}/healing/cancel")
    async def cancel_job_healing(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Cancel self-healing for a job."""
        supervisor = get_supervisor()
        
        cancelled = await supervisor._self_healer.cancel_healing(job_id)
        
        return {
            "success": True,
            "message": f"Healing cancelled for job '{job_id}'" if cancelled else f"No active healing for job '{job_id}'",
            "was_in_progress": cancelled,
        }
    
    @app.get("/api/v1/healing/queue")
    async def get_healing_queue(
        _auth: bool = Depends(verify_token),
    ):
        """Get healing queue status and pending requests."""
        supervisor = get_supervisor()
        
        status = supervisor._self_healer.get_queue_status()
        queue_list = supervisor._self_healer.get_queue_list()
        
        return {
            **status,
            "queue": queue_list,
        }

    @app.delete("/api/v1/runs/{run_id}")
    async def delete_run(
        run_id: int,
        _auth: bool = Depends(verify_token),
    ):
        """Delete a job run and its logs."""
        supervisor = get_supervisor()
        
        # Find the run first
        runs = supervisor.db.get_runs(limit=1000)
        run = next((r for r in runs if r.id == run_id), None)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        
        # Delete logs first
        supervisor.db.delete_logs(run_id=run_id)
        
        # Delete the run
        supervisor.db.delete_run(run_id)
        
        return {
            "success": True,
            "message": f"Run {run_id} deleted",
            "job_id": run.job_id,
        }

    @app.post("/api/v1/runs/{run_id}/extract-session")
    async def extract_run_session(
        run_id: int,
        _auth: bool = Depends(verify_token),
    ):
        """Re-extract OpenClaw session info for a run.
        
        Useful for runs where session info wasn't captured (e.g., killed jobs).
        Does direct lookup via OpenClaw CLI.
        """
        supervisor = get_supervisor()
        
        # Find the run
        runs = supervisor.db.get_runs(limit=1000)
        run = next((r for r in runs if r.id == run_id), None)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        
        # Try to extract session info
        old_key = run.session_key
        old_transcript = run.session_transcript
        
        supervisor._extract_session_info(run.job_id, run)
        
        # Check if we found anything new
        found_new = (run.session_key != old_key) or (run.session_transcript != old_transcript)
        
        return {
            "success": True,
            "run_id": run_id,
            "job_id": run.job_id,
            "session_key": run.session_key,
            "session_transcript": run.session_transcript,
            "found_new": found_new,
            "message": "Session info extracted" if found_new else "No new session info found",
        }

    @app.get("/api/v1/jobs/{job_id}", response_model=JobDetail)
    async def get_job(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Get detailed status of a job."""
        supervisor = get_supervisor()
        job_status = supervisor.get_job_status(job_id)

        if job_status is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        return JobDetail(
            id=job_status["id"],
            name=job_status["name"],
            description=job_status.get("description", ""),
            type=job_status["type"],
            status=job_status["status"],
            enabled=job_status["enabled"],
            pid=job_status.get("pid"),
            started_at=job_status.get("started_at"),
            uptime_seconds=job_status.get("uptime_seconds"),
            next_run=job_status.get("next_run"),
            restart_count=job_status.get("restart_count", 0),
            last_exit_code=job_status.get("last_exit_code"),
            last_error=job_status.get("last_error"),
            cpu_percent=job_status.get("cpu_percent"),
            memory_mb=job_status.get("memory_mb"),
            last_run=job_status.get("last_run"),
            tags=job_status.get("tags", []),
            cmd=job_status.get("cmd"),
            cwd=job_status.get("cwd"),
            schedule=job_status.get("schedule"),
            run_at=job_status.get("run_at"),
            max_retries=job_status.get("max_retries"),
            timeout_seconds=job_status.get("timeout_seconds"),
        )

    @app.post("/api/v1/jobs/{job_id}/start", response_model=ActionResponse)
    async def start_job(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Start a job."""
        supervisor = get_supervisor()

        if supervisor.is_job_running(job_id):
            raise HTTPException(status_code=409, detail=f"Job '{job_id}' is already running")

        success = supervisor.start_job(job_id, trigger="api")

        if not success:
            raise HTTPException(status_code=500, detail=f"Failed to start job '{job_id}'")

        # Get the PID
        state = supervisor.db.get_state(job_id)
        pid = state.pid if state else None

        return ActionResponse(
            success=True,
            message=f"Job '{job_id}' started",
            job_id=job_id,
            pid=pid,
        )

    @app.post("/api/v1/jobs/{job_id}/stop", response_model=ActionResponse)
    async def stop_job(
        job_id: str,
        force: bool = Query(False, description="Force kill"),
        _auth: bool = Depends(verify_token),
    ):
        """Stop a running job."""
        supervisor = get_supervisor()

        if not supervisor.is_job_running(job_id):
            raise HTTPException(status_code=409, detail=f"Job '{job_id}' is not running")

        success = supervisor.stop_job(job_id, force=force)

        if not success:
            raise HTTPException(status_code=500, detail=f"Failed to stop job '{job_id}'")

        return ActionResponse(
            success=True,
            message=f"Job '{job_id}' stopped",
            job_id=job_id,
        )

    @app.post("/api/v1/jobs/{job_id}/restart", response_model=ActionResponse)
    async def restart_job(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Restart a job."""
        supervisor = get_supervisor()

        # Stop if running
        if supervisor.is_job_running(job_id):
            supervisor.stop_job(job_id)
            await asyncio.sleep(1)  # Brief pause

        success = supervisor.start_job(job_id, trigger="api")

        if not success:
            raise HTTPException(status_code=500, detail=f"Failed to restart job '{job_id}'")

        state = supervisor.db.get_state(job_id)
        pid = state.pid if state else None

        return ActionResponse(
            success=True,
            message=f"Job '{job_id}' restarted",
            job_id=job_id,
            pid=pid,
        )

    @app.post("/api/v1/jobs/{job_id}/pause", response_model=ActionResponse)
    async def pause_job(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Pause a job (skip scheduled runs, can be resumed)."""
        from procclaw.models import JobState
        
        supervisor = get_supervisor()
        
        # Check job exists in config
        job = supervisor.jobs.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        # Get or create state
        state = supervisor.db.get_state(job_id)
        if not state:
            state = JobState(job_id=job_id)
        
        if state.paused:
            return ActionResponse(
                success=True,
                message=f"Job '{job_id}' is already paused",
                job_id=job_id,
            )
        
        # If running (continuous job), stop it first
        if supervisor.is_job_running(job_id):
            supervisor.stop_job(job_id)
        
        # Update state
        state.paused = True
        supervisor.db.save_state(state)
        
        # Update scheduler to skip this job
        if supervisor._scheduler:
            supervisor._scheduler.pause_job(job_id)
        
        return ActionResponse(
            success=True,
            message=f"Job '{job_id}' paused",
            job_id=job_id,
        )

    @app.post("/api/v1/jobs/{job_id}/resume", response_model=ActionResponse)
    async def resume_job(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Resume a paused job."""
        from procclaw.models import JobState
        
        supervisor = get_supervisor()
        
        # Check job exists in config
        job = supervisor.jobs.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        # Get or create state
        state = supervisor.db.get_state(job_id)
        if not state:
            state = JobState(job_id=job_id)
        
        if not state.paused:
            return ActionResponse(
                success=True,
                message=f"Job '{job_id}' is not paused",
                job_id=job_id,
            )
        
        # Update state
        state.paused = False
        supervisor.db.save_state(state)
        
        # Re-enable in scheduler
        if supervisor._scheduler:
            supervisor._scheduler.resume_job(job_id)
        
        # For continuous jobs, auto-start after resume
        from procclaw.models import JobType
        if job.type == JobType.CONTINUOUS and not supervisor.is_job_running(job_id):
            supervisor.start_job(job_id, trigger="resume")
        
        return ActionResponse(
            success=True,
            message=f"Job '{job_id}' resumed",
            job_id=job_id,
        )

    @app.delete("/api/v1/jobs/{job_id}")
    async def delete_job(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Delete a job from the configuration."""
        supervisor = get_supervisor()
        
        # Stop if running
        if supervisor.is_job_running(job_id):
            supervisor.stop_job(job_id, force=True)
        
        try:
            success = supervisor.delete_job(job_id)
            if success:
                return {"success": True, "message": f"Job '{job_id}' deleted"}
            else:
                return {"success": False, "error": f"Job '{job_id}' not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.patch("/api/v1/jobs/{job_id}")
    async def update_job(
        job_id: str,
        updates: dict,
        _auth: bool = Depends(verify_token),
    ):
        """Update job attributes."""
        supervisor = get_supervisor()
        
        try:
            success = supervisor.update_job(job_id, updates)
            if success:
                return {"success": True, "message": f"Job '{job_id}' updated"}
            else:
                return {"success": False, "error": f"Job '{job_id}' not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/v1/jobs/{job_id}/tags/{tag}")
    async def add_job_tag(
        job_id: str,
        tag: str,
        _auth: bool = Depends(verify_token),
    ):
        """Add a tag to a job."""
        supervisor = get_supervisor()
        
        try:
            success = supervisor.add_job_tag(job_id, tag)
            return {"success": success}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.delete("/api/v1/jobs/{job_id}/tags/{tag}")
    async def remove_job_tag(
        job_id: str,
        tag: str,
        _auth: bool = Depends(verify_token),
    ):
        """Remove a tag from a job."""
        supervisor = get_supervisor()
        
        try:
            success = supervisor.remove_job_tag(job_id, tag)
            return {"success": success}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/v1/dependencies")
    async def get_dependencies(
        _auth: bool = Depends(verify_token),
    ):
        """Get job dependency graph."""
        supervisor = get_supervisor()
        return supervisor.get_job_dependencies_graph()

    @app.post("/api/v1/jobs/{job_id}/enable")
    async def enable_job(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Enable a job."""
        supervisor = get_supervisor()
        
        try:
            success = supervisor.set_job_enabled(job_id, True)
            return {"success": success}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/v1/jobs/{job_id}/disable")
    async def disable_job(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Disable a job."""
        supervisor = get_supervisor()
        
        # Stop if running
        if supervisor.is_job_running(job_id):
            supervisor.stop_job(job_id)
        
        try:
            success = supervisor.set_job_enabled(job_id, False)
            return {"success": success}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Composite Jobs (chain, group, chord)
    
    @app.post("/api/v1/composite/chain")
    async def run_chain(
        request: ChainRequest,
        _auth: bool = Depends(verify_token),
    ):
        """Run jobs sequentially (A → B → C). Stops on first failure."""
        supervisor = get_supervisor()
        job_ids = request.job_ids
        
        if len(job_ids) < 2:
            raise HTTPException(status_code=400, detail="Chain requires at least 2 jobs")
        
        # Validate all jobs exist
        for job_id in job_ids:
            if not supervisor.jobs.get_job(job_id):
                raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        cid = request.composite_id or f"chain-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        run = await supervisor.run_chain(cid, job_ids, trigger="api")
        
        return {
            "success": run.status.value == "completed",
            "composite_id": cid,
            "type": "chain",
            "status": run.status.value,
            "results": run.results,
        }
    
    @app.post("/api/v1/composite/group")
    async def run_group(
        request: GroupRequest,
        _auth: bool = Depends(verify_token),
    ):
        """Run jobs in parallel (A + B + C). Waits for all to complete."""
        supervisor = get_supervisor()
        job_ids = request.job_ids
        
        if len(job_ids) < 2:
            raise HTTPException(status_code=400, detail="Group requires at least 2 jobs")
        
        # Validate all jobs exist
        for job_id in job_ids:
            if not supervisor.jobs.get_job(job_id):
                raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        cid = request.composite_id or f"group-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        run = await supervisor.run_group(cid, job_ids, trigger="api")
        
        return {
            "success": run.status.value == "completed",
            "composite_id": cid,
            "type": "group",
            "status": run.status.value,
            "results": run.results,
        }
    
    @app.post("/api/v1/composite/chord")
    async def run_chord(
        request: ChordRequest,
        _auth: bool = Depends(verify_token),
    ):
        """Run jobs in parallel, then run callback when all complete."""
        supervisor = get_supervisor()
        job_ids = request.job_ids
        callback = request.callback
        
        if len(job_ids) < 1:
            raise HTTPException(status_code=400, detail="Chord requires at least 1 parallel job")
        
        # Validate all jobs exist
        for job_id in job_ids + [callback]:
            if not supervisor.jobs.get_job(job_id):
                raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        cid = request.composite_id or f"chord-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        run = await supervisor.run_chord(cid, job_ids, callback, trigger="api")
        
        return {
            "success": run.status.value == "completed",
            "composite_id": cid,
            "type": "chord",
            "status": run.status.value,
            "results": run.results,
        }
    
    @app.get("/api/v1/composite")
    async def list_composite_runs(
        _auth: bool = Depends(verify_token),
    ):
        """List all composite job runs."""
        supervisor = get_supervisor()
        runs = supervisor.list_composite_runs()
        
        return {
            "runs": [
                {
                    "composite_id": r.composite_id,
                    "type": r.type,
                    "status": r.status.value,
                    "jobs": r.jobs,
                    "callback": r.callback,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "results": r.results,
                }
                for r in runs
            ],
            "total": len(runs),
        }

    @app.post("/api/v1/daemon/restart")
    async def restart_daemon(_auth: bool = Depends(verify_token)):
        """Restart the daemon (all jobs will be stopped and restarted)."""
        import os
        import signal
        
        # Schedule a delayed restart
        async def do_restart():
            await asyncio.sleep(1)
            os.kill(os.getpid(), signal.SIGTERM)
        
        asyncio.create_task(do_restart())
        return {"success": True, "message": "Daemon restarting..."}

    @app.post("/api/v1/jobs/{job_id}/run", response_model=ActionResponse)
    async def run_job(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Trigger a manual run of a job."""
        supervisor = get_supervisor()

        job = supervisor.jobs.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if supervisor.is_job_running(job_id):
            raise HTTPException(status_code=409, detail=f"Job '{job_id}' is already running")

        success = supervisor.start_job(job_id, trigger="manual")

        if not success:
            raise HTTPException(status_code=500, detail=f"Failed to run job '{job_id}'")

        state = supervisor.db.get_state(job_id)
        pid = state.pid if state else None

        return ActionResponse(
            success=True,
            message=f"Job '{job_id}' triggered",
            job_id=job_id,
            pid=pid,
        )

    @app.get("/api/v1/jobs/{job_id}/logs", response_model=LogsResponse)
    async def get_logs(
        job_id: str,
        lines: int = Query(100, ge=1, le=10000, description="Number of lines"),
        error: bool = Query(False, description="Get error log instead"),
        _auth: bool = Depends(verify_token),
    ):
        """Get job logs. Tries file first, falls back to SQLite if empty."""
        from procclaw.config import DEFAULT_CONFIG_DIR

        supervisor = get_supervisor()
        job = supervisor.jobs.get_job(job_id)

        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if error:
            log_path = job.get_log_stderr_path(DEFAULT_CONFIG_DIR, job_id)
        else:
            log_path = job.get_log_stdout_path(DEFAULT_CONFIG_DIR, job_id)

        # Try reading from file first (for running jobs)
        file_lines = []
        if log_path.exists():
            try:
                with open(log_path, "r") as f:
                    file_lines = [line.rstrip("\n") for line in f.readlines()]
            except Exception:
                pass

        if file_lines:
            return LogsResponse(
                job_id=job_id,
                lines=file_lines[-lines:],
                total_lines=len(file_lines),
                source="file",
            )

        # Fallback to SQLite for historical logs
        level = "stderr" if error else "stdout"
        db_logs = supervisor.db.get_logs(job_id=job_id, level=level, limit=lines)

        if db_logs:
            return LogsResponse(
                job_id=job_id,
                lines=[log["line"] for log in db_logs],
                total_lines=len(db_logs),
                source="sqlite",
            )

        return LogsResponse(job_id=job_id, lines=[], total_lines=0, source="none")

    @app.get("/metrics", response_model=MetricsResponse)
    async def prometheus_metrics():
        """Get Prometheus-format metrics."""
        supervisor = get_supervisor()
        jobs = supervisor.list_jobs()

        lines = []

        # Total jobs
        lines.append("# HELP procclaw_jobs_total Total number of configured jobs")
        lines.append("# TYPE procclaw_jobs_total gauge")
        lines.append(f"procclaw_jobs_total {len(jobs)}")

        # Job status
        lines.append("")
        lines.append("# HELP procclaw_job_status Current status of job (1=running, 0=stopped)")
        lines.append("# TYPE procclaw_job_status gauge")
        for job in jobs:
            value = 1 if job["status"] == "running" else 0
            lines.append(f'procclaw_job_status{{job="{job["id"]}"}} {value}')

        # Job uptime
        lines.append("")
        lines.append("# HELP procclaw_job_uptime_seconds Current uptime of running job")
        lines.append("# TYPE procclaw_job_uptime_seconds gauge")
        for job in jobs:
            if job.get("uptime_seconds"):
                lines.append(f'procclaw_job_uptime_seconds{{job="{job["id"]}"}} {job["uptime_seconds"]:.0f}')

        # Restart count
        lines.append("")
        lines.append("# HELP procclaw_job_restart_count_total Total restarts")
        lines.append("# TYPE procclaw_job_restart_count_total counter")
        for job in jobs:
            lines.append(f'procclaw_job_restart_count_total{{job="{job["id"]}"}} {job.get("restart_count", 0)}')

        return MetricsResponse(text="\n".join(lines))

    @app.post("/api/v1/reload")
    async def reload_config(_auth: bool = Depends(verify_token)):
        """Reload jobs configuration."""
        supervisor = get_supervisor()
        supervisor.reload_jobs()
        return {"success": True, "message": "Configuration reloaded"}

    @app.get("/api/v1/config/jobs")
    async def get_jobs_yaml(_auth: bool = Depends(verify_token)):
        """Get the full jobs.yaml content."""
        from procclaw.config import DEFAULT_JOBS_FILE
        try:
            with open(DEFAULT_JOBS_FILE, "r") as f:
                content = f.read()
            return {"content": content, "path": str(DEFAULT_JOBS_FILE)}
        except FileNotFoundError:
            return {"content": "jobs: {}\n", "path": str(DEFAULT_JOBS_FILE)}

    @app.put("/api/v1/config/jobs")
    async def update_jobs_yaml(
        data: dict,
        _auth: bool = Depends(verify_token),
    ):
        """Update the full jobs.yaml content."""
        import yaml
        from procclaw.config import DEFAULT_JOBS_FILE
        from datetime import datetime
        
        content = data.get("content", "")
        
        # Validate YAML syntax
        try:
            parsed = yaml.safe_load(content)
            if not isinstance(parsed, dict):
                raise HTTPException(status_code=400, detail="YAML must be a dictionary")
        except yaml.YAMLError as e:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")
        
        # Backup current file
        backup_path = DEFAULT_JOBS_FILE.with_suffix(".yaml.bak")
        try:
            if DEFAULT_JOBS_FILE.exists():
                import shutil
                shutil.copy(DEFAULT_JOBS_FILE, backup_path)
        except Exception:
            pass
        
        # Write new content
        with open(DEFAULT_JOBS_FILE, "w") as f:
            f.write(content)
        
        # Reload config
        supervisor = get_supervisor()
        supervisor.reload_jobs()
        
        return {
            "success": True, 
            "message": "Configuration updated and reloaded",
            "backup": str(backup_path),
        }

    # Auth Config Endpoints

    @app.get("/api/v1/config/auth")
    async def get_auth_config(_auth: bool = Depends(verify_token)):
        """Get API authentication configuration."""
        import yaml
        from procclaw.config import DEFAULT_CONFIG_FILE
        
        config_data = {}
        if DEFAULT_CONFIG_FILE.exists():
            with open(DEFAULT_CONFIG_FILE) as f:
                config_data = yaml.safe_load(f) or {}
        
        enabled = config_data.get("api", {}).get("auth", {}).get("enabled", False)
        token = config_data.get("api", {}).get("auth", {}).get("token", "")
        
        # Mask token for display
        token_masked = ""
        if token:
            token_masked = token[:4] + "*" * (len(token) - 8) + token[-4:] if len(token) > 8 else "****"
        
        return {
            "enabled": enabled,
            "token": token,
            "tokenMasked": token_masked,
        }

    @app.post("/api/v1/config/auth/enable")
    async def enable_auth(
        data: dict = None,
        _auth: bool = Depends(verify_token),
    ):
        """Enable API authentication with a new or provided token."""
        import secrets
        import yaml
        from procclaw.config import DEFAULT_CONFIG_FILE
        
        # Generate token if not provided
        token = data.get("token") if data else None
        if not token:
            token = secrets.token_urlsafe(32)
        
        # Load current config
        config_data = {}
        if DEFAULT_CONFIG_FILE.exists():
            with open(DEFAULT_CONFIG_FILE) as f:
                config_data = yaml.safe_load(f) or {}
        
        # Update auth config
        if "api" not in config_data:
            config_data["api"] = {}
        if "auth" not in config_data["api"]:
            config_data["api"]["auth"] = {}
        
        config_data["api"]["auth"]["enabled"] = True
        config_data["api"]["auth"]["token"] = token
        
        # Save config
        with open(DEFAULT_CONFIG_FILE, "w") as f:
            yaml.dump(config_data, f, default_flow_style=False)
        
        # Mask token for response
        token_masked = token[:4] + "*" * (len(token) - 8) + token[-4:] if len(token) > 8 else "****"
        
        return {
            "success": True,
            "message": "Authentication enabled. Restart daemon to apply.",
            "token": token,
            "tokenMasked": token_masked,
            "restartRequired": True,
        }

    @app.post("/api/v1/config/auth/disable")
    async def disable_auth(_auth: bool = Depends(verify_token)):
        """Disable API authentication."""
        import yaml
        from procclaw.config import DEFAULT_CONFIG_FILE
        
        # Load current config
        config_data = {}
        if DEFAULT_CONFIG_FILE.exists():
            with open(DEFAULT_CONFIG_FILE) as f:
                config_data = yaml.safe_load(f) or {}
        
        # Update auth config
        if "api" not in config_data:
            config_data["api"] = {}
        if "auth" not in config_data["api"]:
            config_data["api"]["auth"] = {}
        
        config_data["api"]["auth"]["enabled"] = False
        
        # Save config
        with open(DEFAULT_CONFIG_FILE, "w") as f:
            yaml.dump(config_data, f, default_flow_style=False)
        
        return {
            "success": True,
            "message": "Authentication disabled. Restart daemon to apply.",
            "restartRequired": True,
        }

    # DLQ Endpoints

    @app.get("/api/v1/dlq")
    async def list_dlq(
        pending_only: bool = Query(True, description="Only show pending entries"),
        _auth: bool = Depends(verify_token),
    ):
        """List dead letter queue entries."""
        supervisor = get_supervisor()
        entries = supervisor.get_dlq_entries(pending_only=pending_only)
        return {
            "entries": [
                {
                    "id": e.id,
                    "job_id": e.job_id,
                    "failed_at": e.failed_at.isoformat(),
                    "attempts": e.attempts,
                    "last_error": e.last_error,
                    "is_reinjected": e.is_reinjected,
                    "reinjected_at": e.reinjected_at.isoformat() if e.reinjected_at else None,
                }
                for e in entries
            ],
            "total": len(entries),
        }

    @app.get("/api/v1/dlq/stats")
    async def dlq_stats(_auth: bool = Depends(verify_token)):
        """Get DLQ statistics."""
        supervisor = get_supervisor()
        return supervisor.get_dlq_stats()

    @app.post("/api/v1/dlq/{entry_id}/reinject")
    async def reinject_dlq(
        entry_id: int,
        _auth: bool = Depends(verify_token),
    ):
        """Reinject a DLQ entry for retry."""
        supervisor = get_supervisor()
        success = supervisor.reinject_dlq_entry(entry_id)
        
        if not success:
            raise HTTPException(status_code=500, detail=f"Failed to reinject DLQ entry {entry_id}")
        
        return {"success": True, "message": f"DLQ entry {entry_id} reinjected"}

    @app.delete("/api/v1/dlq")
    async def purge_dlq(
        job_id: str | None = Query(None, description="Only purge for this job"),
        older_than_days: int | None = Query(None, description="Only purge older than N days"),
        _auth: bool = Depends(verify_token),
    ):
        """Purge DLQ entries."""
        supervisor = get_supervisor()
        count = supervisor.purge_dlq(job_id=job_id, older_than_days=older_than_days)
        return {"success": True, "purged": count}

    # Webhook Trigger Endpoint

    @app.post("/api/v1/trigger/{job_id}")
    async def webhook_trigger(
        job_id: str,
        payload: dict | None = None,
        idempotency_key: str | None = Query(None, description="Idempotency key"),
        credentials: HTTPAuthorizationCredentials | None = Security(security),
    ):
        """Trigger a job via webhook."""
        supervisor = get_supervisor()
        
        job = supervisor.jobs.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        if not job.trigger.enabled or job.trigger.type.value != "webhook":
            raise HTTPException(status_code=400, detail=f"Job '{job_id}' does not have webhook trigger enabled")
        
        # Extract auth token
        auth_token = credentials.credentials if credentials else None
        
        success = supervisor.trigger_job_webhook(
            job_id=job_id,
            payload=payload,
            idempotency_key=idempotency_key,
            auth_token=auth_token,
        )
        
        if not success:
            raise HTTPException(status_code=500, detail=f"Failed to trigger job '{job_id}'")
        
        state = supervisor.db.get_state(job_id)
        pid = state.pid if state else None
        
        return ActionResponse(
            success=True,
            message=f"Job '{job_id}' triggered via webhook",
            job_id=job_id,
            pid=pid,
        )

    # Concurrency Endpoint

    @app.get("/api/v1/jobs/{job_id}/concurrency")
    async def job_concurrency(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Get concurrency stats for a job."""
        supervisor = get_supervisor()
        
        job = supervisor.jobs.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        return supervisor.get_concurrency_stats(job_id)

    # ETA Scheduling Endpoints

    @app.post("/api/v1/jobs/{job_id}/schedule")
    async def schedule_job(
        job_id: str,
        run_at: str | None = None,
        run_in: int | None = None,
        timezone: str | None = None,
        _auth: bool = Depends(verify_token),
    ):
        """Schedule a job to run at a specific time.
        
        Args:
            run_at: ISO datetime when to run
            run_in: Seconds from now to run
            timezone: Timezone for run_at
        """
        supervisor = get_supervisor()
        
        job = supervisor.jobs.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        
        if run_at:
            eta_job = supervisor.schedule_job_at(job_id, run_at, timezone=timezone)
        elif run_in:
            eta_job = supervisor.schedule_job_in(job_id, run_in)
        else:
            raise HTTPException(status_code=400, detail="Must provide run_at or run_in")
        
        return {
            "success": True,
            "job_id": job_id,
            "run_at": eta_job.run_at.isoformat(),
            "seconds_until": eta_job.seconds_until,
        }

    @app.delete("/api/v1/jobs/{job_id}/schedule")
    async def cancel_schedule(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Cancel an ETA-scheduled job."""
        supervisor = get_supervisor()
        
        cancelled = supervisor.cancel_eta(job_id)
        
        if not cancelled:
            raise HTTPException(status_code=404, detail=f"No ETA schedule for job '{job_id}'")
        
        return {"success": True, "message": f"ETA schedule for '{job_id}' cancelled"}

    @app.get("/api/v1/eta")
    async def list_eta(
        _auth: bool = Depends(verify_token),
    ):
        """List all ETA-scheduled jobs."""
        supervisor = get_supervisor()
        
        eta_jobs = supervisor.get_eta_jobs()
        
        return {
            "jobs": [
                {
                    "job_id": j.job_id,
                    "run_at": j.run_at.isoformat(),
                    "scheduled_at": j.scheduled_at.isoformat(),
                    "seconds_until": j.seconds_until,
                    "is_due": j.is_due,
                }
                for j in eta_jobs
            ],
            "total": len(eta_jobs),
        }

    # Revocation Endpoints

    @app.post("/api/v1/jobs/{job_id}/revoke")
    async def revoke_job(
        job_id: str,
        reason: str | None = None,
        terminate: bool = False,
        expires_in: int | None = None,
        _auth: bool = Depends(verify_token),
    ):
        """Revoke a job (cancel if queued/scheduled, optionally terminate if running)."""
        supervisor = get_supervisor()
        
        revocation = supervisor.revoke_job(
            job_id=job_id,
            reason=reason,
            terminate=terminate,
            expires_in=expires_in,
        )
        
        return {
            "success": True,
            "job_id": job_id,
            "revoked_at": revocation.revoked_at.isoformat(),
            "terminate": revocation.terminate,
            "expires_at": revocation.expires_at.isoformat() if revocation.expires_at else None,
        }

    @app.delete("/api/v1/jobs/{job_id}/revoke")
    async def unrevoke_job(
        job_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Remove a job revocation."""
        supervisor = get_supervisor()
        
        unrevoked = supervisor.unrevoke_job(job_id)
        
        if not unrevoked:
            raise HTTPException(status_code=404, detail=f"No revocation for job '{job_id}'")
        
        return {"success": True, "message": f"Revocation for '{job_id}' removed"}

    @app.get("/api/v1/revocations")
    async def list_revocations(
        _auth: bool = Depends(verify_token),
    ):
        """List all active revocations."""
        supervisor = get_supervisor()
        
        revocations = supervisor.get_revocations()
        
        return {
            "revocations": [
                {
                    "job_id": r.job_id,
                    "revoked_at": r.revoked_at.isoformat(),
                    "reason": r.reason,
                    "terminate": r.terminate,
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                }
                for r in revocations
            ],
            "total": len(revocations),
        }

    # Workflow Endpoints

    @app.get("/api/v1/workflows")
    async def list_workflows(
        _auth: bool = Depends(verify_token),
    ):
        """List all registered workflows."""
        supervisor = get_supervisor()
        
        workflows = supervisor.list_workflows()
        
        return {
            "workflows": [w.to_dict() for w in workflows],
            "total": len(workflows),
        }

    @app.get("/api/v1/workflows/{workflow_id}")
    async def get_workflow(
        workflow_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Get workflow details."""
        supervisor = get_supervisor()
        
        workflow = supervisor.get_workflow(workflow_id)
        
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
        
        return workflow.to_dict()

    @app.post("/api/v1/workflows/{workflow_id}/run")
    async def run_workflow(
        workflow_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Start a workflow run."""
        supervisor = get_supervisor()
        
        run = supervisor.start_workflow(workflow_id)
        
        if not run:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
        
        return {
            "success": True,
            "workflow_id": workflow_id,
            "run_id": run.id,
            "status": run.status.value,
        }

    @app.get("/api/v1/workflows/{workflow_id}/runs")
    async def list_workflow_runs(
        workflow_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """List runs for a workflow."""
        supervisor = get_supervisor()
        
        runs = supervisor.list_workflow_runs(workflow_id)
        
        return {
            "runs": [r.to_dict() for r in runs],
            "total": len(runs),
        }

    @app.get("/api/v1/workflow-runs/{run_id}")
    async def get_workflow_run(
        run_id: int,
        _auth: bool = Depends(verify_token),
    ):
        """Get workflow run details."""
        supervisor = get_supervisor()
        
        run = supervisor.get_workflow_run(run_id)
        
        if not run:
            raise HTTPException(status_code=404, detail=f"Workflow run {run_id} not found")
        
        return run.to_dict()

    @app.post("/api/v1/workflow-runs/{run_id}/cancel")
    async def cancel_workflow_run(
        run_id: int,
        _auth: bool = Depends(verify_token),
    ):
        """Cancel a running workflow."""
        supervisor = get_supervisor()
        
        cancelled = supervisor.cancel_workflow(run_id)
        
        if not cancelled:
            raise HTTPException(status_code=400, detail=f"Cannot cancel workflow run {run_id}")
        
        return {"success": True, "message": f"Workflow run {run_id} cancelled"}

    # Results Endpoint

    @app.get("/api/v1/jobs/{job_id}/results")
    async def get_job_results(
        job_id: str,
        run_id: int | None = None,
        _auth: bool = Depends(verify_token),
    ):
        """Get job results."""
        supervisor = get_supervisor()
        
        if run_id:
            result = supervisor.get_job_result(job_id, run_id)
        else:
            result = supervisor.get_last_job_result(job_id)
        
        if not result:
            raise HTTPException(status_code=404, detail=f"No results for job '{job_id}'")
        
        return result.to_dict()

    # =========================================================================
    # Static Files & Web UI
    # =========================================================================

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """Serve the web UI."""
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return HTMLResponse("<h1>ProcClaw</h1><p>Web UI not found</p>")

    # Mount static files if directory exists
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # =========================================================================
    # Additional Endpoints for Web UI
    # =========================================================================

    @app.get("/api/v1/daemon/logs")
    async def get_daemon_logs(
        lines: int = Query(100, description="Number of lines"),
        type: str = Query("stdout", description="Log type: stdout or stderr"),
        _auth: bool = Depends(verify_token),
    ):
        """Get daemon logs."""
        from procclaw.config import DEFAULT_LOGS_DIR
        
        log_file = DEFAULT_LOGS_DIR / f"daemon.{'error.' if type == 'stderr' else ''}log"
        
        if not log_file.exists():
            return {"job_id": "daemon", "lines": [], "total_lines": 0}
        
        try:
            with open(log_file, "r") as f:
                all_lines = f.readlines()
                return {
                    "job_id": "daemon",
                    "lines": [l.rstrip() for l in all_lines[-lines:]],
                    "total_lines": len(all_lines),
                }
        except Exception as e:
            return {"job_id": "daemon", "lines": [f"Error reading logs: {e}"], "total_lines": 0}

    @app.get("/api/v1/dlq")
    async def list_dlq(
        pending_only: bool = Query(False),
        job_id: str | None = Query(None),
        _auth: bool = Depends(verify_token),
    ):
        """List DLQ entries."""
        supervisor = get_supervisor()
        
        try:
            entries = supervisor.list_dlq_entries(pending_only=pending_only, job_id=job_id)
            return {"entries": entries, "total": len(entries)}
        except Exception:
            return {"entries": [], "total": 0}

    @app.get("/api/v1/dlq/stats")
    async def dlq_stats(_auth: bool = Depends(verify_token)):
        """Get DLQ statistics."""
        supervisor = get_supervisor()
        
        try:
            stats = supervisor.get_dlq_stats()
            return stats
        except Exception:
            return {"pending": 0, "total": 0}

    @app.get("/api/v1/stats/recent")
    async def recent_stats(_auth: bool = Depends(verify_token)):
        """Get recent run statistics (last 24h)."""
        supervisor = get_supervisor()
        
        try:
            stats = supervisor.get_recent_stats(hours=24)
            return stats
        except Exception:
            return {"success": 0, "failed": 0, "runs": 0}

    @app.post("/api/v1/dlq/{entry_id}/reinject")
    async def reinject_dlq(
        entry_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Reinject a DLQ entry."""
        supervisor = get_supervisor()
        
        try:
            success = supervisor.reinject_dlq_entry(entry_id)
            return {"success": success, "entry_id": entry_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.delete("/api/v1/dlq/{entry_id}")
    async def delete_dlq(
        entry_id: str,
        _auth: bool = Depends(verify_token),
    ):
        """Delete a DLQ entry."""
        supervisor = get_supervisor()
        
        try:
            success = supervisor.delete_dlq_entry(entry_id)
            return {"success": success, "entry_id": entry_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.delete("/api/v1/dlq")
    async def purge_dlq(
        older_than_days: int = Query(7),
        job_id: str | None = Query(None),
        _auth: bool = Depends(verify_token),
    ):
        """Purge old DLQ entries."""
        supervisor = get_supervisor()
        
        try:
            count = supervisor.purge_dlq(older_than_days=older_than_days, job_id=job_id)
            return {"success": True, "purged": count}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return app


async def run_server(supervisor: "Supervisor", host: str = "127.0.0.1", port: int = 9876) -> None:
    """Run the API server."""
    import uvicorn

    set_supervisor(supervisor)
    app = create_app()

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",  # Reduce uvicorn noise
    )
    server = uvicorn.Server(config)
    await server.serve()
