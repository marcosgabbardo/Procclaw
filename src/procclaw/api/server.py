"""FastAPI server for ProcClaw API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

if TYPE_CHECKING:
    from procclaw.core.supervisor import Supervisor

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
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> bool:
    """Verify the API token if authentication is enabled."""
    if _supervisor is None:
        return True

    config = _supervisor.config
    if not config.api.auth.enabled:
        return True

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
    pid: int | None = None
    started_at: str | None = None
    uptime_seconds: float | None = None
    next_run: str | None = None
    restart_count: int = 0
    tags: list[str] = []


class JobDetail(JobSummary):
    description: str = ""
    last_exit_code: int | None = None
    last_error: str | None = None
    cpu_percent: float | None = None
    memory_mb: float | None = None
    last_run: dict | None = None


class JobListResponse(BaseModel):
    jobs: list[JobSummary]
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


class MetricsResponse(BaseModel):
    text: str


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
        _auth: bool = Depends(verify_token),
    ):
        """List all jobs."""
        supervisor = get_supervisor()
        jobs = supervisor.list_jobs()

        # Apply filters
        if status:
            jobs = [j for j in jobs if j["status"] == status]
        if tag:
            jobs = [j for j in jobs if tag in j.get("tags", [])]

        return JobListResponse(
            jobs=[
                JobSummary(
                    id=j["id"],
                    name=j["name"],
                    type=j["type"],
                    status=j["status"],
                    enabled=j["enabled"],
                    pid=j.get("pid"),
                    started_at=j.get("started_at"),
                    uptime_seconds=j.get("uptime_seconds"),
                    next_run=j.get("next_run"),
                    restart_count=j.get("restart_count", 0),
                    tags=j.get("tags", []),
                )
                for j in jobs
            ],
            total=len(jobs),
        )

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
        """Get job logs."""
        from procclaw.config import DEFAULT_CONFIG_DIR

        supervisor = get_supervisor()
        job = supervisor.jobs.get_job(job_id)

        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if error:
            log_path = job.get_log_stderr_path(DEFAULT_CONFIG_DIR, job_id)
        else:
            log_path = job.get_log_stdout_path(DEFAULT_CONFIG_DIR, job_id)

        if not log_path.exists():
            return LogsResponse(job_id=job_id, lines=[], total_lines=0)

        # Read last N lines
        with open(log_path, "r") as f:
            all_lines = f.readlines()

        result_lines = all_lines[-lines:]

        return LogsResponse(
            job_id=job_id,
            lines=[line.rstrip("\n") for line in result_lines],
            total_lines=len(all_lines),
        )

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
