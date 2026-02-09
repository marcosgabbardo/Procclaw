"""Pydantic models for ProcClaw configuration and state."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class JobType(str, Enum):
    """Type of job execution."""

    SCHEDULED = "scheduled"
    CONTINUOUS = "continuous"
    MANUAL = "manual"
    ONESHOT = "oneshot"  # Run once at specific datetime, then auto-disable
    OPENCLAW = "openclaw"  # Jobs linked to OpenClaw (depend on AI/cron integration)
    # Composite types
    CHAIN = "chain"  # Sequential execution (A → B → C)
    GROUP = "group"  # Parallel execution (A + B + C)
    CHORD = "chord"  # Parallel + callback (A + B + C) → D


class JobStatus(str, Enum):
    """Current status of a job."""

    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    PLANNED = "planned"  # Scheduled job waiting for next run
    PENDING = "pending"  # Waiting for dependency
    DISABLED = "disabled"


class HealthCheckType(str, Enum):
    """Type of health check."""

    PROCESS = "process"
    HTTP = "http"
    FILE = "file"


class OnOverlap(str, Enum):
    """Behavior when scheduled job overlaps."""

    SKIP = "skip"
    QUEUE = "queue"
    KILL_RESTART = "kill_restart"


class DependencyCondition(str, Enum):
    """Condition for job dependency."""

    AFTER_START = "after_start"
    AFTER_COMPLETE = "after_complete"
    BEFORE_COMPLETE = "before_complete"


class RetryPreset(str, Enum):
    """Preset retry policies."""

    WEBHOOK = "webhook"  # 0, 30s, 5m, 15m, 1h
    EXPONENTIAL = "exponential"  # 1s, 2s, 4s, 8s... cap 5m
    FIXED = "fixed"  # 30s, 30s, 30s...


# Retry delay presets in seconds
RETRY_PRESETS: dict[RetryPreset, list[int]] = {
    RetryPreset.WEBHOOK: [0, 30, 300, 900, 3600],
    RetryPreset.EXPONENTIAL: [1, 2, 4, 8, 16, 32, 64, 128, 256, 300],
    RetryPreset.FIXED: [30, 30, 30, 30, 30],
}


class HealthCheckConfig(BaseModel):
    """Health check configuration."""

    type: HealthCheckType = HealthCheckType.PROCESS
    interval: int = 60  # seconds
    timeout: int = 10  # seconds

    # HTTP-specific
    url: str | None = None
    method: str = "GET"
    expected_status: int = 200
    expected_body: str | None = None

    # File-specific
    path: str | None = None
    max_age: int = 120  # seconds


class RetryConfig(BaseModel):
    """Retry policy configuration."""

    enabled: bool = True
    max_attempts: int = 5
    preset: RetryPreset = RetryPreset.WEBHOOK
    delays: list[int] | None = None  # Custom delays in seconds
    
    # Rate limit specific settings
    rate_limit_delays: list[int] = Field(
        default_factory=lambda: [60, 120, 300, 600, 1800]  # 1m, 2m, 5m, 10m, 30m
    )
    rate_limit_max_attempts: int = 5

    def get_delays(self) -> list[int]:
        """Get the delay sequence for retries."""
        if self.delays:
            return self.delays
        return RETRY_PRESETS[self.preset]
    
    def get_rate_limit_delays(self) -> list[int]:
        """Get the delay sequence for rate limit retries."""
        return self.rate_limit_delays


class ShutdownConfig(BaseModel):
    """Graceful shutdown configuration."""

    grace_period: int = 60  # seconds
    signal: str = "SIGTERM"


class LogConfig(BaseModel):
    """Logging configuration for a job."""

    stdout: str | None = None
    stderr: str | None = None
    max_size: str = "10MB"
    rotate: int = 5


class OperatingHoursAction(str, Enum):
    """Action to take outside operating hours."""
    
    PAUSE = "pause"      # Pause the job (restart when hours resume)
    SKIP = "skip"        # Skip scheduled runs
    ALERT = "alert"      # Alert but still run


class ResourceLimitAction(str, Enum):
    """Action to take when resource limit is exceeded."""
    
    WARN = "warn"        # Just log a warning
    THROTTLE = "throttle"  # Reduce CPU priority
    KILL = "kill"        # Kill the job


class ResourceLimitsConfig(BaseModel):
    """Resource limits configuration for a job."""
    
    enabled: bool = False
    max_memory_mb: int | None = None  # Maximum memory in MB
    max_cpu_percent: int | None = None  # Maximum CPU percentage (averaged)
    timeout_seconds: int | None = None  # Maximum runtime in seconds
    action: ResourceLimitAction = ResourceLimitAction.WARN
    
    # How often to check resources (seconds)
    check_interval: int = 30


class OperatingHoursConfig(BaseModel):
    """Operating hours configuration for a job."""
    
    enabled: bool = False
    days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])  # 1=Mon, 7=Sun
    start: str = "08:00"  # HH:MM
    end: str = "18:00"    # HH:MM
    timezone: str = "America/Sao_Paulo"
    action: OperatingHoursAction = OperatingHoursAction.SKIP


class Priority(int, Enum):
    """Job execution priority."""
    
    CRITICAL = 0  # Highest priority, may preempt others
    HIGH = 1
    NORMAL = 2
    LOW = 3  # Lowest priority


class DedupConfig(BaseModel):
    """Deduplication configuration for a job."""
    
    enabled: bool = True
    window_seconds: int = 60  # Don't run same job within this window
    use_params: bool = True  # Include params in fingerprint


class ConcurrencyConfig(BaseModel):
    """Concurrency configuration for a job."""
    
    max_instances: int = 1  # Max concurrent instances of this job
    queue_excess: bool = True  # Queue jobs that exceed max (vs reject)
    queue_timeout: int = 300  # Max time to wait in queue (seconds)


class LockConfig(BaseModel):
    """Distributed lock configuration for a job."""
    
    enabled: bool = False
    timeout_seconds: int = 300  # Lock timeout
    retry_interval: float = 0.5  # Seconds between lock attempts
    max_attempts: int = 10  # Max lock acquisition attempts


class TriggerType(str, Enum):
    """Type of event trigger."""
    
    WEBHOOK = "webhook"  # HTTP POST trigger
    FILE = "file"  # File creation trigger
    # QUEUE = "queue"  # Message queue trigger (future)


class TriggerConfig(BaseModel):
    """Event trigger configuration for a job."""
    
    enabled: bool = False
    type: TriggerType = TriggerType.WEBHOOK
    
    # Webhook settings
    auth_token: str | None = None  # Optional bearer token
    
    # File trigger settings
    watch_path: str | None = None  # Directory to watch
    pattern: str = "*"  # Glob pattern for files
    delete_after: bool = False  # Delete file after trigger


class AlertConfig(BaseModel):
    """Alert configuration for a job."""

    on_failure: bool = True
    on_restart: bool = False
    on_max_retries: bool = True
    on_health_fail: bool = False
    on_recovered: bool = False
    channels: list[str] = Field(default_factory=lambda: ["whatsapp"])


class JobDependency(BaseModel):
    """Dependency on another job."""

    job: str
    condition: DependencyCondition = DependencyCondition.AFTER_START
    timeout: int = 60  # seconds
    fail_on_dependency_failure: bool = True


class JobConfig(BaseModel):
    """Configuration for a single job."""

    # Required
    name: str
    cmd: str

    # Optional with defaults
    description: str = ""
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    type: JobType = JobType.MANUAL
    enabled: bool = True

    # Scheduling (for scheduled jobs)
    schedule: str | None = None  # cron expression
    run_at: datetime | None = None  # ISO datetime for oneshot jobs
    timezone: str = "America/Sao_Paulo"
    on_overlap: OnOverlap = OnOverlap.SKIP

    # Retry and shutdown
    retry: RetryConfig = Field(default_factory=RetryConfig)
    shutdown: ShutdownConfig = Field(default_factory=ShutdownConfig)

    # Health check
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)

    # Logging
    log: LogConfig = Field(default_factory=LogConfig)

    # Alerts
    alerts: AlertConfig = Field(default_factory=AlertConfig)

    # Dependencies
    depends_on: list[JobDependency] = Field(default_factory=list)

    # Operating hours
    operating_hours: OperatingHoursConfig = Field(default_factory=OperatingHoursConfig)

    # Resource limits
    resources: ResourceLimitsConfig = Field(default_factory=ResourceLimitsConfig)

    # Priority
    priority: Priority = Priority.NORMAL

    # Deduplication
    dedup: DedupConfig = Field(default_factory=DedupConfig)

    # Concurrency control
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)

    # Distributed locks
    lock: LockConfig = Field(default_factory=LockConfig)

    # Event triggers
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)

    # Metadata
    tags: list[str] = Field(default_factory=list)
    
    # Composite jobs (chain, group, chord)
    steps: list[str] = Field(default_factory=list)  # For CHAIN: sequential job IDs
    jobs: list[str] = Field(default_factory=list)  # For GROUP: parallel job IDs
    callback: str | None = None  # For CHORD: job to run after group completes

    @field_validator("schedule")
    @classmethod
    def validate_schedule_for_type(cls, v: str | None, info: Any) -> str | None:
        """Validate that scheduled jobs have a schedule."""
        # Note: Full validation happens at config load time
        return v

    def get_log_stdout_path(self, base_dir: Path, job_id: str) -> Path:
        """Get the stdout log path for this job."""
        if self.log.stdout:
            return Path(self.log.stdout).expanduser()
        return base_dir / f"{job_id}.log"

    def get_log_stderr_path(self, base_dir: Path, job_id: str) -> Path:
        """Get the stderr log path for this job."""
        if self.log.stderr:
            return Path(self.log.stderr).expanduser()
        return base_dir / f"{job_id}.error.log"


class JobState(BaseModel):
    """Runtime state of a job."""

    job_id: str
    status: JobStatus = JobStatus.STOPPED
    pid: int | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    restart_count: int = 0
    retry_attempt: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    next_run: datetime | None = None  # For scheduled jobs
    next_retry: datetime | None = None  # For retry scheduling


class JobRun(BaseModel):
    """Record of a single job run."""

    id: int | None = None
    job_id: str
    started_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    trigger: str = "manual"  # manual, scheduled, restart, dependency
    error: str | None = None
    composite_id: str | None = None  # workflow ID if run as part of chain/group/chord


class FileLogMode(str, Enum):
    """File logging mode."""
    
    KEEP = "keep"       # Keep log files (default)
    DELETE = "delete"   # Delete after saving to SQLite
    DISABLED = "disabled"  # No file logging (stream to SQLite only - not implemented)


class DaemonConfig(BaseModel):
    """Daemon configuration."""

    host: str = "127.0.0.1"
    port: int = 9876
    log_level: str = "INFO"
    file_log_mode: FileLogMode = FileLogMode.KEEP


class ApiAuthConfig(BaseModel):
    """API authentication configuration."""

    enabled: bool = False
    token: str | None = None


class ApiConfig(BaseModel):
    """API configuration."""

    auth: ApiAuthConfig = Field(default_factory=ApiAuthConfig)


class DefaultsConfig(BaseModel):
    """Default configuration for jobs."""

    retry: RetryConfig = Field(default_factory=RetryConfig)
    shutdown: ShutdownConfig = Field(default_factory=ShutdownConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    log: LogConfig = Field(default_factory=LogConfig)


class OpenClawConfig(BaseModel):
    """OpenClaw integration configuration."""

    enabled: bool = True
    memory_logging: bool = True
    sync_cron: bool = True
    alerts_enabled: bool = True
    alerts_channels: list[str] = Field(default_factory=lambda: ["whatsapp"])


class ProcClawConfig(BaseModel):
    """Main ProcClaw configuration."""

    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    openclaw: OpenClawConfig = Field(default_factory=OpenClawConfig)


class JobsConfig(BaseModel):
    """Jobs configuration file."""

    jobs: dict[str, JobConfig] = Field(default_factory=dict)

    def get_job(self, job_id: str) -> JobConfig | None:
        """Get a job by ID."""
        return self.jobs.get(job_id)

    def get_enabled_jobs(self) -> dict[str, JobConfig]:
        """Get all enabled jobs."""
        return {k: v for k, v in self.jobs.items() if v.enabled}

    def get_jobs_by_type(self, job_type: JobType) -> dict[str, JobConfig]:
        """Get jobs by type."""
        return {k: v for k, v in self.jobs.items() if v.type == job_type and v.enabled}

    def get_jobs_by_tag(self, tag: str) -> dict[str, JobConfig]:
        """Get jobs by tag."""
        return {k: v for k, v in self.jobs.items() if tag in v.tags and v.enabled}
