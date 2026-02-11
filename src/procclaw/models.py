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
    QUEUED = "queued"  # Waiting in execution queue


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


class SessionTriggerEvent(str, Enum):
    """Events that can trigger a session message."""
    
    ON_SUCCESS = "on_success"
    ON_FAILURE = "on_failure"
    ON_START = "on_start"
    ON_COMPLETE = "on_complete"  # Both success and failure


class SessionTrigger(BaseModel):
    """Trigger to send a message to an OpenClaw session.
    
    Template variables available in message:
    - {{job_id}}: Job identifier
    - {{job_name}}: Job display name
    - {{status}}: "success" or "failed"
    - {{exit_code}}: Process exit code
    - {{duration}}: Duration in seconds
    - {{error}}: Error message (if failed)
    - {{started_at}}: Start timestamp
    - {{finished_at}}: End timestamp
    """
    
    event: SessionTriggerEvent
    session: str = "main"  # "main", "cron:<id>", or full session key
    message: str  # Message template
    enabled: bool = True
    immediate: bool = False  # If True, try to wake agent for immediate delivery


class HealingAction(str, Enum):
    """Allowed actions for self-healing remediation."""
    
    RESTART_JOB = "restart_job"
    EDIT_SCRIPT = "edit_script"
    EDIT_CONFIG = "edit_config"
    RUN_COMMAND = "run_command"
    EDIT_OPENCLAW_CRON = "edit_openclaw_cron"
    RESTART_SERVICE = "restart_service"


class HealingStatus(str, Enum):
    """Status of a healing attempt."""
    
    IN_PROGRESS = "in_progress"
    FIXED = "fixed"
    GAVE_UP = "gave_up"
    AWAITING_APPROVAL = "awaiting_approval"


# ============================================================================
# Self-Healing v2 - New Enums
# ============================================================================

class HealingMode(str, Enum):
    """Mode of self-healing operation."""
    
    REACTIVE = "reactive"      # Only triggered on job failure
    PROACTIVE = "proactive"    # Periodic behavior review


class ReviewFrequency(str, Enum):
    """Frequency of proactive healing reviews."""
    
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    ON_FAILURE = "on_failure"
    ON_SLA_BREACH = "on_sla_breach"
    MANUAL = "manual"


class SuggestionCategory(str, Enum):
    """Category of a healing suggestion."""
    
    PERFORMANCE = "performance"
    COST = "cost"
    RELIABILITY = "reliability"
    SECURITY = "security"
    CONFIG = "config"
    PROMPT = "prompt"
    SCRIPT = "script"


class SuggestionSeverity(str, Enum):
    """Severity level of a healing suggestion."""
    
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SuggestionStatus(str, Enum):
    """Status of a healing suggestion."""
    
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"


class ActionStatus(str, Enum):
    """Status of a healing action execution."""
    
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ReviewStatus(str, Enum):
    """Status of a healing review."""
    
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class HealingAnalysisConfig(BaseModel):
    """Configuration for failure analysis in self-healing."""
    
    include_logs: bool = True
    log_lines: int = 200
    include_stderr: bool = True
    include_history: int = 5  # Number of recent runs to include
    include_config: bool = True


class HealingRemediationConfig(BaseModel):
    """Configuration for auto-remediation in self-healing."""
    
    enabled: bool = True
    max_attempts: int = 3
    allowed_actions: list[HealingAction] = Field(
        default_factory=lambda: [HealingAction.RESTART_JOB]
    )
    forbidden_paths: list[str] = Field(default_factory=list)
    require_approval: bool = False


class HealingNotifyConfig(BaseModel):
    """Notification settings for self-healing."""
    
    on_analysis: bool = False
    on_fix_attempt: bool = True
    on_success: bool = True
    on_give_up: bool = True
    session: str = "main"


# ============================================================================
# Self-Healing v2 - New Config Models
# ============================================================================

class ReviewScheduleConfig(BaseModel):
    """Schedule configuration for proactive healing reviews."""
    
    frequency: ReviewFrequency = ReviewFrequency.DAILY
    time: str = "03:00"  # HH:MM format for daily/weekly
    day: int = 1  # 0-6 for weekly (0=Sunday)
    min_runs: int = 5  # Minimum runs since last review to trigger


class ReviewScopeConfig(BaseModel):
    """Scope configuration for what to analyze during healing reviews."""
    
    analyze_logs: bool = True
    analyze_runs: bool = True
    analyze_ai_sessions: bool = True
    analyze_sla: bool = True
    analyze_workflows: bool = True
    analyze_script: bool = True
    analyze_prompt: bool = True
    analyze_config: bool = True


class SuggestionBehaviorConfig(BaseModel):
    """Configuration for how suggestions are handled."""
    
    auto_apply: bool = False  # If True, apply suggestions automatically
    auto_apply_categories: list[str] = Field(default_factory=list)  # Categories to auto-apply even if auto_apply=False
    min_severity_for_approval: str = "medium"  # Suggestions below this severity auto-apply
    notify_on_suggestion: bool = True
    notify_channel: str = "whatsapp"


class SelfHealingConfig(BaseModel):
    """Self-healing configuration for a job.
    
    When enabled, failed jobs are automatically analyzed and 
    remediation is attempted based on the configuration.
    
    v2 adds proactive mode for periodic behavior reviews.
    """
    
    enabled: bool = False
    
    # v2: Mode of operation (reactive=on failure only, proactive=periodic review)
    mode: HealingMode = HealingMode.REACTIVE
    
    # UI preset for remediation settings (conservative, moderate, aggressive, custom)
    preset: str = "conservative"
    
    # v2: Schedule for proactive reviews
    review_schedule: ReviewScheduleConfig = Field(default_factory=ReviewScheduleConfig)
    
    # v2: What to analyze during reviews
    review_scope: ReviewScopeConfig = Field(default_factory=ReviewScopeConfig)
    
    # v2: How to handle suggestions
    suggestions: SuggestionBehaviorConfig = Field(default_factory=SuggestionBehaviorConfig)
    
    # Existing: Analysis configuration
    analysis: HealingAnalysisConfig = Field(default_factory=HealingAnalysisConfig)
    
    # Existing: Remediation configuration
    remediation: HealingRemediationConfig = Field(default_factory=HealingRemediationConfig)
    
    # Existing: Notification configuration
    notify: HealingNotifyConfig = Field(default_factory=HealingNotifyConfig)


# Hardcoded forbidden paths that can NEVER be modified by self-healing
HEALING_FORBIDDEN_PATHS_ALWAYS: list[str] = [
    # ProcClaw source - NEVER MODIFY
    "~/.openclaw/workspace/projects/procclaw/",
    "**/projects/procclaw/**",
    
    # OpenClaw source - NEVER MODIFY
    "**/node_modules/openclaw/**",
    "/opt/homebrew/lib/node_modules/openclaw/",
    "/usr/local/lib/node_modules/openclaw/",
    
    # System critical
    "~/.ssh/",
    "~/.gnupg/",
    "~/.openclaw/openclaw.json",
    "/etc/",
    "/usr/",
    "/bin/",
    "/sbin/",
]


class JobDependency(BaseModel):
    """Dependency on another job."""

    job: str
    condition: DependencyCondition = DependencyCondition.AFTER_START
    timeout: int = 60  # seconds
    fail_on_dependency_failure: bool = True


class ErrorBudgetPeriod(str, Enum):
    """Error budget period."""
    
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class ErrorBudgetConfig(BaseModel):
    """Error budget configuration."""
    
    period: ErrorBudgetPeriod = ErrorBudgetPeriod.WEEK
    max_failures: int = 3


class SLAConfig(BaseModel):
    """Service Level Agreement configuration for a job."""
    
    enabled: bool = False
    
    # Core metrics thresholds
    success_rate: float = 95.0  # % minimum success rate
    schedule_tolerance: int = 300  # seconds tolerance for schedule adherence (5 min)
    max_duration: int | None = None  # seconds maximum run duration
    
    # Advanced metrics
    max_consecutive_failures: int | None = None
    error_budget: ErrorBudgetConfig | None = None
    
    # Evaluation
    evaluation_period: str = "7d"  # Period for SLA calculation
    
    # Alerting
    alert_on_breach: bool = True
    alert_threshold: float = 90.0  # Alert when score drops below this


class SLAStatus(str, Enum):
    """SLA status for a run."""
    
    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"
    NO_SLA = "no_sla"


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

    # Event triggers (incoming)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    
    # Session triggers (outgoing - send to OpenClaw)
    session_triggers: list[SessionTrigger] = Field(default_factory=list)

    # Self-healing (AI-powered failure analysis and auto-remediation)
    self_healing: SelfHealingConfig = Field(default_factory=SelfHealingConfig)

    # SLA (Service Level Agreement)
    sla: SLAConfig = Field(default_factory=SLAConfig)

    # Metadata
    tags: list[str] = Field(default_factory=list)
    
    # Execution queue (jobs in same queue run sequentially)
    queue: str | None = None
    
    # OpenClaw job settings
    model: str | None = None  # Model for OpenClaw jobs (e.g., "sonnet", "opus")
    thinking: str | None = None  # Thinking level: off, minimal, low, medium, high
    
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
    paused: bool = False  # Temporarily paused (runtime, persists in state)
    queued_at: datetime | None = None  # When job was added to execution queue


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
    session_key: str | None = None  # OpenClaw session key (for openclaw jobs)
    session_transcript: str | None = None  # Path to OpenClaw session transcript
    session_messages: str | None = None  # JSON with session messages (persisted in DB)
    
    # SLA fields
    sla_snapshot_id: int | None = None  # Reference to job_sla_snapshots
    sla_status: str | None = None  # pass, fail, partial, no_sla
    sla_details: str | None = None  # JSON with SLA check details
    
    # Self-healing fields
    healing_status: str | None = None  # in_progress, fixed, gave_up, awaiting_approval
    healing_attempts: int = 0
    healing_session_key: str | None = None  # Session key for healing transcript
    healing_result: dict | None = None  # JSON with analysis + actions_taken
    original_exit_code: int | None = None  # Exit code before healing fixed it


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


# ============================================================================
# Self-Healing v2 - Data Models (for DB rows)
# ============================================================================

class HealingReview(BaseModel):
    """Data model for a healing review record."""
    
    id: int
    job_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: ReviewStatus = ReviewStatus.RUNNING
    runs_analyzed: int = 0
    logs_lines: int = 0
    ai_sessions_count: int = 0
    sla_violations_count: int = 0
    suggestions_count: int = 0
    auto_applied_count: int = 0
    error_message: str | None = None
    analysis_duration_ms: int | None = None
    ai_tokens_used: int | None = None
    created_at: datetime


class HealingSuggestion(BaseModel):
    """Data model for a healing suggestion record."""
    
    id: int
    review_id: int
    job_id: str
    category: SuggestionCategory
    severity: SuggestionSeverity
    title: str
    description: str
    current_state: str | None = None
    suggested_change: str | None = None
    expected_impact: str | None = None
    affected_files: list[str] = Field(default_factory=list)
    # Pre-generated content: AI generates the proposed changes during analysis
    # so the user can review EXACTLY what will be applied before approving
    proposed_content: str | None = None  # The new file content (full or diff)
    target_file: str | None = None  # Which file will be modified
    status: SuggestionStatus = SuggestionStatus.PENDING
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None  # 'auto' or 'human'
    rejection_reason: str | None = None
    applied_at: datetime | None = None
    action_id: int | None = None
    created_at: datetime


class HealingActionRecord(BaseModel):
    """Data model for a healing action record."""
    
    id: int
    suggestion_id: int
    job_id: str
    action_type: str  # 'edit_script', 'edit_prompt', 'edit_config', 'run_command', 'restart_job'
    file_path: str | None = None
    original_content: str | None = None
    new_content: str | None = None
    command_executed: str | None = None
    status: ActionStatus = ActionStatus.SUCCESS
    error_message: str | None = None
    can_rollback: bool = True
    rolled_back_at: datetime | None = None
    execution_duration_ms: int | None = None
    ai_session_key: str | None = None
    created_at: datetime
