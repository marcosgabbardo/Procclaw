# Self-Healing v2 - Pydantic Models

## Overview
New and updated Pydantic models for Self-Healing v2 configuration.

## New Enums

### HealingMode
```python
class HealingMode(str, Enum):
    REACTIVE = "reactive"      # Only on failure
    PROACTIVE = "proactive"    # Periodic analysis
```

### ReviewFrequency
```python
class ReviewFrequency(str, Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    ON_FAILURE = "on_failure"
    ON_SLA_BREACH = "on_sla_breach"
    MANUAL = "manual"
```

### SuggestionCategory
```python
class SuggestionCategory(str, Enum):
    PERFORMANCE = "performance"
    COST = "cost"
    RELIABILITY = "reliability"
    SECURITY = "security"
    CONFIG = "config"
    PROMPT = "prompt"
    SCRIPT = "script"
```

### SuggestionSeverity
```python
class SuggestionSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
```

### SuggestionStatus
```python
class SuggestionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"
```

### ActionStatus
```python
class ActionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
```

## New Config Models

### ReviewScheduleConfig
```python
class ReviewScheduleConfig(BaseModel):
    frequency: ReviewFrequency = ReviewFrequency.DAILY
    time: str = "03:00"  # HH:MM format
    day: int = 1  # 0-6 for weekly (0=Sunday)
    min_runs: int = 5  # Minimum runs since last review to trigger
```

### ReviewScopeConfig
```python
class ReviewScopeConfig(BaseModel):
    analyze_logs: bool = True
    analyze_runs: bool = True
    analyze_ai_sessions: bool = True
    analyze_sla: bool = True
    analyze_workflows: bool = True
    analyze_script: bool = True
    analyze_prompt: bool = True
    analyze_config: bool = True
```

### SuggestionBehaviorConfig
```python
class SuggestionBehaviorConfig(BaseModel):
    auto_apply: bool = False
    auto_apply_categories: list[str] = []  # Categories that auto-apply even if auto_apply=False
    min_severity_for_approval: str = "medium"  # 'low' auto-applies, rest needs approval
    notify_on_suggestion: bool = True
    notify_channel: str = "whatsapp"
```

## Updated SelfHealingConfig

```python
class SelfHealingConfig(BaseModel):
    """Self-healing configuration for a job - v2."""
    
    enabled: bool = False
    
    # NEW: Mode of operation
    mode: HealingMode = HealingMode.REACTIVE
    
    # NEW: Review schedule (for proactive mode)
    review_schedule: ReviewScheduleConfig = Field(default_factory=ReviewScheduleConfig)
    
    # NEW: What to analyze
    review_scope: ReviewScopeConfig = Field(default_factory=ReviewScopeConfig)
    
    # NEW: How to handle suggestions
    suggestions: SuggestionBehaviorConfig = Field(default_factory=SuggestionBehaviorConfig)
    
    # EXISTING: Analysis config
    analysis: HealingAnalysisConfig = Field(default_factory=HealingAnalysisConfig)
    
    # EXISTING: Remediation config  
    remediation: HealingRemediationConfig = Field(default_factory=HealingRemediationConfig)
    
    # EXISTING: Notification config
    notify: HealingNotifyConfig = Field(default_factory=HealingNotifyConfig)
```

## Data Models (for DB rows)

### HealingReview
```python
class HealingReview(BaseModel):
    id: int
    job_id: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    runs_analyzed: int = 0
    logs_lines: int = 0
    ai_sessions_count: int = 0
    sla_violations_count: int = 0
    suggestions_count: int = 0
    auto_applied_count: int = 0
    error_message: str | None
    analysis_duration_ms: int | None
    ai_tokens_used: int | None
    created_at: datetime
```

### HealingSuggestion
```python
class HealingSuggestion(BaseModel):
    id: int
    review_id: int
    job_id: str
    category: SuggestionCategory
    severity: SuggestionSeverity
    title: str
    description: str
    current_state: str | None
    suggested_change: str | None
    expected_impact: str | None
    affected_files: list[str] = []
    status: SuggestionStatus = SuggestionStatus.PENDING
    reviewed_at: datetime | None
    reviewed_by: str | None
    rejection_reason: str | None
    applied_at: datetime | None
    action_id: int | None
    created_at: datetime
```

### HealingAction
```python
class HealingAction(BaseModel):
    id: int
    suggestion_id: int
    job_id: str
    action_type: str
    file_path: str | None
    original_content: str | None
    new_content: str | None
    command_executed: str | None
    status: ActionStatus
    error_message: str | None
    can_rollback: bool = True
    rolled_back_at: datetime | None
    execution_duration_ms: int | None
    ai_session_key: str | None
    created_at: datetime
```

## Acceptance Criteria
- [ ] All enums defined and importable
- [ ] All config models with proper defaults
- [ ] All data models for DB rows
- [ ] Backwards compatible with existing jobs.yaml
- [ ] Pydantic validation works correctly
- [ ] JSON serialization/deserialization works
