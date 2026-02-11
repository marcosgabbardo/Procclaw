"""SLA (Service Level Agreement) calculation and tracking for ProcClaw jobs."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from croniter import croniter
from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database
    from procclaw.models import JobConfig, JobRun, SLAConfig


# Default SLA configs by job type
DEFAULT_SLA_BY_TYPE = {
    "continuous": {
        "success_rate": 99.0,
        "max_duration": None,
        "schedule_tolerance": None,  # Not applicable
    },
    "scheduled": {
        "success_rate": 95.0,
        "schedule_tolerance": 300,  # 5 min
        "max_duration": 3600,  # 1 hour
    },
    "openclaw": {
        "success_rate": 90.0,
        "schedule_tolerance": 600,  # 10 min
        "max_duration": 900,  # 15 min
    },
    "oneshot": {
        "success_rate": 100.0,
        "schedule_tolerance": 600,
        "max_duration": 7200,  # 2 hours
    },
    "manual": {
        "success_rate": 95.0,
        "schedule_tolerance": None,  # Not applicable
        "max_duration": None,
    },
}


@dataclass
class SLACheckResult:
    """Result of checking a single run against SLA."""
    
    run_id: int
    job_id: str
    status: str  # "pass", "fail", "partial"
    
    # Individual checks
    success_check: bool | None = None  # Did the run succeed?
    on_time_check: bool | None = None  # Did it start on time?
    duration_check: bool | None = None  # Did it finish within max duration?
    
    # Details
    expected_start: datetime | None = None
    actual_start: datetime | None = None
    delay_seconds: float | None = None
    duration_seconds: float | None = None
    max_duration: int | None = None
    exit_code: int | None = None
    
    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps({
            "status": self.status,
            "success_check": self.success_check,
            "on_time_check": self.on_time_check,
            "duration_check": self.duration_check,
            "expected_start": self.expected_start.isoformat() if self.expected_start else None,
            "actual_start": self.actual_start.isoformat() if self.actual_start else None,
            "delay_seconds": self.delay_seconds,
            "duration_seconds": self.duration_seconds,
            "max_duration": self.max_duration,
            "exit_code": self.exit_code,
        })


@dataclass
class SLAMetrics:
    """Aggregated SLA metrics for a job over a period."""
    
    job_id: str
    period_start: datetime
    period_end: datetime
    
    # Counts
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    late_starts: int = 0
    over_duration: int = 0
    
    # Calculated rates (0-100)
    success_rate: float = 0.0
    schedule_adherence: float = 0.0
    duration_compliance: float = 0.0
    
    # Overall score (weighted average)
    overall_score: float = 0.0
    
    # Status
    status: str = "no_data"  # "healthy", "warning", "critical", "no_data"
    
    # Duration stats
    avg_duration: float | None = None
    p50_duration: float | None = None
    p95_duration: float | None = None
    max_duration_actual: float | None = None
    
    # Runs with SLA details
    runs: list[dict] = field(default_factory=list)
    
    # Trend
    trend: str = "stable"  # "improving", "stable", "degrading"
    
    # Last breach
    last_breach: datetime | None = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API response."""
        return {
            "job_id": self.job_id,
            "period": {
                "start": self.period_start.isoformat(),
                "end": self.period_end.isoformat(),
            },
            "total_runs": self.total_runs,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "late_starts": self.late_starts,
            "over_duration": self.over_duration,
            "success_rate": round(self.success_rate, 1),
            "schedule_adherence": round(self.schedule_adherence, 1),
            "duration_compliance": round(self.duration_compliance, 1),
            "overall_score": round(self.overall_score, 1),
            "status": self.status,
            "avg_duration": round(self.avg_duration, 1) if self.avg_duration else None,
            "p50_duration": round(self.p50_duration, 1) if self.p50_duration else None,
            "p95_duration": round(self.p95_duration, 1) if self.p95_duration else None,
            "max_duration_actual": round(self.max_duration_actual, 1) if self.max_duration_actual else None,
            "trend": self.trend,
            "last_breach": self.last_breach.isoformat() if self.last_breach else None,
            "runs": self.runs,
        }


def parse_period(period_str: str) -> timedelta:
    """Parse a period string like '7d', '24h', '30d' to timedelta."""
    match = re.match(r"(\d+)([dhwm])", period_str.lower())
    if not match:
        return timedelta(days=7)  # Default
    
    value = int(match.group(1))
    unit = match.group(2)
    
    if unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    elif unit == "w":
        return timedelta(weeks=value)
    elif unit == "m":
        return timedelta(days=value * 30)  # Approximate
    
    return timedelta(days=7)


def get_effective_sla(job: "JobConfig") -> dict:
    """Get effective SLA config for a job, using defaults if not configured."""
    defaults = DEFAULT_SLA_BY_TYPE.get(job.type.value, DEFAULT_SLA_BY_TYPE["manual"])
    
    if not job.sla.enabled:
        return {
            "enabled": False,
            **defaults,
        }
    
    return {
        "enabled": True,
        "success_rate": job.sla.success_rate,
        "schedule_tolerance": job.sla.schedule_tolerance,
        "max_duration": job.sla.max_duration or defaults.get("max_duration"),
        "max_consecutive_failures": job.sla.max_consecutive_failures,
        "evaluation_period": job.sla.evaluation_period,
        "alert_on_breach": job.sla.alert_on_breach,
        "alert_threshold": job.sla.alert_threshold,
    }


def get_expected_start_time(
    job: "JobConfig",
    run_started_at: datetime,
) -> datetime | None:
    """Get the expected start time for a run based on job schedule.
    
    For scheduled jobs, finds the most recent schedule tick before the run.
    For manual/oneshot jobs, returns None (no expected time).
    """
    if job.type.value not in ("scheduled", "openclaw") or not job.schedule:
        return None
    
    try:
        # Get the schedule tick that should have triggered this run
        # We look backwards from the run start to find the most recent tick
        cron = croniter(job.schedule, run_started_at - timedelta(hours=24))
        
        # Find ticks within a reasonable window before the run
        expected = None
        for _ in range(100):  # Safety limit
            tick = cron.get_next(datetime)
            if tick > run_started_at:
                break
            expected = tick
        
        return expected
    except Exception as e:
        logger.warning(f"Failed to parse schedule for {job.name}: {e}")
        return None


def check_run_sla(
    run: "JobRun",
    job: "JobConfig",
    sla_config: dict | None = None,
) -> SLACheckResult:
    """Check a single run against SLA criteria.
    
    Args:
        run: The job run to check
        job: The job configuration
        sla_config: Optional override SLA config (for historical checks)
    
    Returns:
        SLACheckResult with pass/fail status and details
    """
    if sla_config is None:
        sla_config = get_effective_sla(job)
    
    result = SLACheckResult(
        run_id=run.id or 0,
        job_id=run.job_id,
        status="pass",
        exit_code=run.exit_code,
        duration_seconds=run.duration_seconds,
    )
    
    checks_passed = 0
    checks_total = 0
    
    # 1. Success check
    if run.finished_at is not None:  # Only check completed runs
        checks_total += 1
        result.success_check = run.exit_code == 0
        if result.success_check:
            checks_passed += 1
    
    # 2. Schedule adherence check (for scheduled jobs)
    schedule_tolerance = sla_config.get("schedule_tolerance")
    if schedule_tolerance is not None and job.schedule:
        checks_total += 1
        expected = get_expected_start_time(job, run.started_at)
        result.expected_start = expected
        result.actual_start = run.started_at
        
        if expected:
            delay = (run.started_at - expected).total_seconds()
            result.delay_seconds = delay
            result.on_time_check = delay <= schedule_tolerance
            if result.on_time_check:
                checks_passed += 1
        else:
            # Can't determine expected time, assume OK
            result.on_time_check = True
            checks_passed += 1
    
    # 3. Duration check
    max_duration = sla_config.get("max_duration")
    if max_duration is not None and run.duration_seconds is not None:
        checks_total += 1
        result.max_duration = max_duration
        result.duration_check = run.duration_seconds <= max_duration
        if result.duration_check:
            checks_passed += 1
    
    # Determine overall status
    if checks_total == 0:
        result.status = "no_sla"
    elif checks_passed == checks_total:
        result.status = "pass"
    elif checks_passed == 0:
        result.status = "fail"
    else:
        result.status = "partial"
    
    return result


def calculate_sla_metrics(
    job_id: str,
    job: "JobConfig",
    runs: list["JobRun"],
    period_start: datetime,
    period_end: datetime,
) -> SLAMetrics:
    """Calculate SLA metrics for a job over a period.
    
    Args:
        job_id: Job identifier
        job: Job configuration
        runs: List of runs in the period
        period_start: Start of evaluation period
        period_end: End of evaluation period
    
    Returns:
        SLAMetrics with calculated values
    """
    sla_config = get_effective_sla(job)
    
    metrics = SLAMetrics(
        job_id=job_id,
        period_start=period_start,
        period_end=period_end,
    )
    
    if not runs:
        metrics.status = "no_data"
        return metrics
    
    # Check each run
    durations = []
    run_details = []
    
    for run in runs:
        check = check_run_sla(run, job, sla_config)
        
        metrics.total_runs += 1
        
        if run.exit_code == 0:
            metrics.successful_runs += 1
        else:
            metrics.failed_runs += 1
        
        if check.on_time_check is False:
            metrics.late_starts += 1
            if metrics.last_breach is None or run.started_at > metrics.last_breach:
                metrics.last_breach = run.started_at
        
        if check.duration_check is False:
            metrics.over_duration += 1
            if metrics.last_breach is None or run.started_at > metrics.last_breach:
                metrics.last_breach = run.started_at
        
        if check.success_check is False:
            if metrics.last_breach is None or run.started_at > metrics.last_breach:
                metrics.last_breach = run.started_at
        
        if run.duration_seconds is not None:
            durations.append(run.duration_seconds)
        
        run_details.append({
            "run_id": run.id,
            "started_at": run.started_at.isoformat(),
            "sla_status": check.status,
            "on_time": check.on_time_check,
            "delay_seconds": check.delay_seconds,
            "duration": run.duration_seconds,
            "success": check.success_check,
            "exit_code": run.exit_code,
        })
    
    # Calculate rates
    if metrics.total_runs > 0:
        metrics.success_rate = (metrics.successful_runs / metrics.total_runs) * 100
        
        # Schedule adherence (only count runs where we could check)
        scheduled_runs = sum(1 for r in run_details if r.get("on_time") is not None)
        if scheduled_runs > 0:
            on_time_runs = scheduled_runs - metrics.late_starts
            metrics.schedule_adherence = (on_time_runs / scheduled_runs) * 100
        else:
            metrics.schedule_adherence = 100.0  # No schedule = always on time
        
        # Duration compliance
        duration_checked = sum(1 for r in run_details if r.get("duration") is not None)
        if duration_checked > 0:
            within_sla = duration_checked - metrics.over_duration
            metrics.duration_compliance = (within_sla / duration_checked) * 100
        else:
            metrics.duration_compliance = 100.0  # No duration = always compliant
    
    # Calculate overall score (weighted average)
    # Weights: success_rate 50%, schedule_adherence 30%, duration_compliance 20%
    metrics.overall_score = (
        metrics.success_rate * 0.5 +
        metrics.schedule_adherence * 0.3 +
        metrics.duration_compliance * 0.2
    )
    
    # Duration statistics
    if durations:
        metrics.avg_duration = statistics.mean(durations)
        metrics.max_duration_actual = max(durations)
        
        sorted_durations = sorted(durations)
        n = len(sorted_durations)
        metrics.p50_duration = sorted_durations[n // 2]
        metrics.p95_duration = sorted_durations[int(n * 0.95)] if n >= 20 else sorted_durations[-1]
    
    # Determine status
    threshold_warning = sla_config.get("alert_threshold", 90.0)
    threshold_critical = threshold_warning - 10.0  # 10% below warning = critical
    
    if metrics.overall_score >= sla_config.get("success_rate", 95.0):
        metrics.status = "healthy"
    elif metrics.overall_score >= threshold_warning:
        metrics.status = "warning"
    else:
        metrics.status = "critical"
    
    metrics.runs = run_details
    
    return metrics


def calculate_job_sla_score(
    db: "Database",
    job_id: str,
    job: "JobConfig",
    period: str = "7d",
) -> SLAMetrics:
    """Calculate SLA score for a job.
    
    Args:
        db: Database instance
        job_id: Job identifier
        job: Job configuration
        period: Evaluation period (e.g., "7d", "24h", "30d")
    
    Returns:
        SLAMetrics with calculated values
    """
    period_delta = parse_period(period)
    period_end = datetime.now()
    period_start = period_end - period_delta
    
    # Get runs in the period
    runs = db.get_runs(job_id=job_id, limit=1000, since=period_start)
    
    # Filter to only completed runs in the period
    runs = [
        r for r in runs
        if r.finished_at is not None and r.started_at >= period_start
    ]
    
    return calculate_sla_metrics(job_id, job, runs, period_start, period_end)


def create_config_hash(job: "JobConfig") -> str:
    """Create a hash of job config for snapshot comparison."""
    config_str = json.dumps({
        "cmd": job.cmd,
        "schedule": job.schedule,
        "type": job.type.value,
        "sla": {
            "enabled": job.sla.enabled,
            "success_rate": job.sla.success_rate,
            "schedule_tolerance": job.sla.schedule_tolerance,
            "max_duration": job.sla.max_duration,
        },
    }, sort_keys=True)
    return hashlib.sha256(config_str.encode()).hexdigest()[:16]


def save_sla_snapshot(
    db: "Database",
    job_id: str,
    job: "JobConfig",
) -> int:
    """Save a snapshot of job config for SLA tracking.
    
    Returns snapshot ID.
    """
    import sqlite3
    
    config_hash = create_config_hash(job)
    config_json = job.model_dump_json()
    sla_json = json.dumps({
        "enabled": job.sla.enabled,
        "success_rate": job.sla.success_rate,
        "schedule_tolerance": job.sla.schedule_tolerance,
        "max_duration": job.sla.max_duration,
        "max_consecutive_failures": job.sla.max_consecutive_failures,
    })
    
    with db._connect() as conn:
        # Try to find existing snapshot with same hash
        cursor = conn.execute(
            "SELECT id FROM job_sla_snapshots WHERE job_id = ? AND config_hash = ?",
            (job_id, config_hash),
        )
        row = cursor.fetchone()
        if row:
            return row[0]
        
        # Create new snapshot
        cursor = conn.execute(
            """
            INSERT INTO job_sla_snapshots (job_id, snapshot_at, config_hash, config_json, sla_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, datetime.now().isoformat(), config_hash, config_json, sla_json),
        )
        return cursor.lastrowid or 0


def get_sla_snapshot(db: "Database", snapshot_id: int) -> dict | None:
    """Get SLA snapshot by ID."""
    with db._connect() as conn:
        cursor = conn.execute(
            "SELECT * FROM job_sla_snapshots WHERE id = ?",
            (snapshot_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "snapshot_at": row["snapshot_at"],
            "config_hash": row["config_hash"],
            "config_json": row["config_json"],
            "sla_json": row["sla_json"],
        }
