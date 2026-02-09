# ProcClaw Workflow Features - Implementation Plan

## Overview

Adding 4 Celery-inspired features while maintaining ProcClaw's process-manager identity.

---

## 1. ETA Scheduling

### Design
Jobs can specify a one-time execution at a specific datetime, in addition to cron.

```yaml
jobs:
  one-shot-task:
    cmd: "python deploy.py"
    run_at: "2024-01-15T10:00:00"  # ISO 8601
    timezone: "America/Sao_Paulo"
```

### Implementation
- Add `run_at: datetime | None` to JobConfig
- Scheduler checks both cron AND run_at
- After run_at triggers, job becomes manual (doesn't repeat)
- Support relative times via API: `run_in: 3600` (seconds)

### Files
- `models.py`: Add `run_at` field
- `scheduler.py`: Check run_at in scheduling loop
- `api/server.py`: Add endpoint to schedule job at time

### API
```
POST /api/v1/jobs/{job_id}/schedule
{
  "run_at": "2024-01-15T10:00:00",
  "run_in": 3600  // alternative: seconds from now
}
```

---

## 2. Task Revocation

### Design
Cancel jobs that are:
- Queued (waiting for concurrency slot)
- Scheduled (ETA not yet reached)
- NOT running (can't revoke in-progress)

```python
supervisor.revoke("job-id")  # Cancel if queued/scheduled
supervisor.revoke("job-id", terminate=True)  # Also kill if running
```

### Implementation
- Add revocation registry (set of revoked job_ids)
- Check registry before starting job
- Clear from queue when revoked
- Optional: terminate running job

### Files
- `core/revocation.py`: Revocation manager
- `supervisor.py`: Check revocation before start
- `api/server.py`: Revoke endpoint

### API
```
POST /api/v1/jobs/{job_id}/revoke
{
  "terminate": false  // optional: also kill running
}
```

---

## 3. Task Composition

### Design
Three primitives (inspired by Celery Canvas):

#### Chain
Sequential execution, result passed to next job via env var.

```yaml
workflows:
  deploy-pipeline:
    type: chain
    jobs:
      - build
      - test  
      - deploy
    on_failure: stop  # or continue
```

#### Group
Parallel execution, all jobs start together.

```yaml
workflows:
  parallel-tests:
    type: group
    jobs:
      - test-unit
      - test-integration
      - test-e2e
```

#### Chord
Group + callback when all complete.

```yaml
workflows:
  process-and-notify:
    type: chord
    jobs:
      - process-a
      - process-b
      - process-c
    callback: notify-complete
```

### Implementation

#### Data Model
```python
class WorkflowType(str, Enum):
    CHAIN = "chain"
    GROUP = "group"
    CHORD = "chord"

class WorkflowConfig(BaseModel):
    name: str
    type: WorkflowType
    jobs: list[str]  # Job IDs
    callback: str | None = None  # For chord
    on_failure: str = "stop"  # stop, continue, retry
    pass_results: bool = True  # Pass output to next

class WorkflowRun(BaseModel):
    id: int
    workflow_id: str
    started_at: datetime
    finished_at: datetime | None
    status: str  # running, completed, failed
    job_runs: list[int]  # Job run IDs
    results: dict[str, Any]  # Collected results
```

#### Execution Logic

**Chain:**
1. Start first job
2. On complete, capture exit code + output
3. Pass to next job via env: `PROCCLAW_PREV_RESULT`, `PROCCLAW_PREV_EXIT`
4. If failure and on_failure=stop, abort chain

**Group:**
1. Start all jobs simultaneously
2. Track all job states
3. Complete when all finish
4. Collect all results

**Chord:**
1. Run as Group
2. When group completes, start callback job
3. Pass collected results to callback

### Files
- `models.py`: WorkflowConfig, WorkflowRun
- `core/workflow.py`: WorkflowManager
- `config.py`: Load workflows from workflows.yaml
- `supervisor.py`: Integrate workflow execution
- `api/server.py`: Workflow endpoints
- `db.py`: workflow_runs table

### API
```
POST /api/v1/workflows/{workflow_id}/run
GET /api/v1/workflows/{workflow_id}/status
POST /api/v1/workflows/{workflow_id}/cancel
```

---

## 4. Result Aggregation

### Design
Capture job outputs for use in workflows and callbacks.

#### Result Capture
- Capture stdout last N lines
- Capture exit code
- Capture custom output (JSON to file)
- Store in database

#### Result Passing
- Chain: Previous job result to next job
- Chord: All job results to callback
- Env vars: `PROCCLAW_RESULTS_FILE` points to JSON

### Implementation

```python
class JobResult(BaseModel):
    job_id: str
    run_id: int
    exit_code: int
    stdout_tail: str  # Last 1000 chars
    output_file: str | None  # Custom output JSON
    duration_seconds: float
    finished_at: datetime

class ResultCollector:
    def capture(self, job_id: str, run_id: int) -> JobResult
    def get_chain_results(self, workflow_run_id: int) -> list[JobResult]
    def get_group_results(self, workflow_run_id: int) -> dict[str, JobResult]
```

### Files
- `models.py`: JobResult model
- `core/results.py`: ResultCollector
- `supervisor.py`: Capture results on job complete
- `db.py`: job_results table

---

## Database Schema Additions

```sql
-- ETA scheduling
ALTER TABLE job_configs ADD COLUMN run_at TIMESTAMP;

-- Revocation
CREATE TABLE revoked_jobs (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL,
    revoked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reason TEXT,
    expires_at TIMESTAMP  -- Auto-clear after time
);

-- Workflows
CREATE TABLE workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    config TEXT NOT NULL,  -- JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE workflow_runs (
    id INTEGER PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT DEFAULT 'running',
    current_step INTEGER DEFAULT 0,
    results TEXT,  -- JSON
    FOREIGN KEY (workflow_id) REFERENCES workflows(id)
);

-- Results
CREATE TABLE job_results (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    workflow_run_id INTEGER,
    exit_code INTEGER,
    stdout_tail TEXT,
    output_file TEXT,
    duration_seconds REAL,
    finished_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES job_runs(id),
    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id)
);
```

---

## Implementation Order

1. **ETA Scheduling** (simplest, standalone)
2. **Task Revocation** (needed for workflows)
3. **Result Aggregation** (needed for workflows)
4. **Task Composition** (depends on all above)

---

## Test Strategy

### Unit Tests
- ETA: Parse datetime, timezone handling, run_in conversion
- Revocation: Add/check/expire revocations
- Results: Capture stdout, parse output file
- Workflow: State machine transitions

### Integration Tests
- ETA + Scheduler: Job triggers at correct time
- Revocation + Queue: Revoked job not started
- Chain: Jobs run in sequence, results passed
- Group: Jobs run in parallel
- Chord: Callback runs after group

### Stress Tests
- 100 concurrent workflow runs
- Chain with 50 steps
- Group with 100 parallel jobs
- Revoke during execution

### Mission-Critical Tests
- Chain failure at step N stops correctly
- Revocation race conditions
- Result capture on crash
- Workflow recovery after daemon restart

---

## Timeline

| Phase | Feature | Estimated |
|-------|---------|-----------|
| 1 | ETA Scheduling | 20 min |
| 2 | Task Revocation | 20 min |
| 3 | Result Aggregation | 25 min |
| 4 | Task Composition | 40 min |
| 5 | Tests | 30 min |
| 6 | Skill Update | 10 min |

Total: ~2.5 hours

---

## Success Criteria

- [ ] All 4 features implemented
- [ ] 100% test pass rate
- [ ] No race conditions
- [ ] Daemon restart recovery
- [ ] Skill documentation updated
- [ ] README updated
