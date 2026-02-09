"""FastAPI server for ProcClaw API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Query, Security
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


class RunSummary(BaseModel):
    id: int
    job_id: str
    job_name: str | None = None
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    trigger: str
    status: str  # running, success, failed
    error: str | None = None
    cmd: str | None = None


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
            ))
        
        return RunListResponse(runs=result, total=len(result))

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

    @app.get("/api/v1/jobs/{job_id}/logs")
    async def get_job_logs(
        job_id: str,
        lines: int = Query(100, description="Number of lines"),
        type: str = Query("stdout", description="Log type: stdout or stderr"),
        _auth: bool = Depends(verify_token),
    ):
        """Get job logs."""
        from procclaw.config import DEFAULT_LOGS_DIR
        
        log_file = DEFAULT_LOGS_DIR / f"{job_id}.{'error.' if type == 'stderr' else ''}log"
        
        if not log_file.exists():
            return {"job_id": job_id, "lines": [], "total_lines": 0}
        
        try:
            with open(log_file, "r") as f:
                all_lines = f.readlines()
                return {
                    "job_id": job_id,
                    "lines": [l.rstrip() for l in all_lines[-lines:]],
                    "total_lines": len(all_lines),
                }
        except Exception as e:
            return {"job_id": job_id, "lines": [f"Error reading logs: {e}"], "total_lines": 0}

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
