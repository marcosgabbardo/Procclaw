# ProcClaw Gaps Implementation Plan

## Overview
Implementing 6 critical gaps to make ProcClaw production-ready.

---

## 1. Dead-Letter Queue (DLQ)

### Design
- New table `dead_letter_jobs` in SQLite
- Jobs moved to DLQ when `max_retries` reached
- API/CLI to list, inspect, and reinject DLQ jobs
- Configurable: `dlq.enabled`, `dlq.retention_days`

### Files
- `src/procclaw/core/dlq.py` - DLQ manager
- Update `db.py` - new table + methods
- Update `models.py` - DLQEntry model
- Update `retry.py` - move to DLQ on max_retries
- Update `api/server.py` - DLQ endpoints
- Update `cli/main.py` - DLQ commands

### Schema
```sql
CREATE TABLE dead_letter_jobs (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL,
    original_run_id INTEGER,
    failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    job_config TEXT,  -- JSON snapshot
    reinjected_at TIMESTAMP,
    reinjected_run_id INTEGER
);
```

---

## 2. Distributed Locks

### Design
- Abstract `LockProvider` interface
- Implementations: `LocalLock`, `FileLock`, `RedisLock` (optional)
- Lock acquired before job start, released after
- Lock timeout with auto-release
- Configurable per-job: `lock.enabled`, `lock.timeout`

### Files
- `src/procclaw/core/locks.py` - Lock providers
- Update `models.py` - LockConfig
- Update `supervisor.py` - acquire/release locks

### Interface
```python
class LockProvider(ABC):
    async def acquire(self, key: str, timeout: int) -> bool
    async def release(self, key: str) -> bool
    async def is_locked(self, key: str) -> bool
```

---

## 3. Priority Queues

### Design
- 4 priority levels: CRITICAL(0), HIGH(1), NORMAL(2), LOW(3)
- Jobs sorted by priority, then by scheduled time
- Priority configurable per-job
- Preemption optional (critical can pause lower)

### Files
- Update `models.py` - Priority enum, job priority field
- `src/procclaw/core/priority_queue.py` - Priority-aware queue
- Update `scheduler.py` - priority sorting
- Update `supervisor.py` - priority-based execution

---

## 4. Max Concurrent

### Design
- Per-job: `concurrency.max_instances`
- Global: `concurrency.global_max`
- Semaphore-based limiting
- Queue jobs that exceed limit

### Files
- `src/procclaw/core/concurrency.py` - Concurrency limiter
- Update `models.py` - ConcurrencyConfig
- Update `supervisor.py` - check before start

---

## 5. Event Triggers

### Design
- Trigger types: `webhook`, `file`, `queue`
- Webhook: POST endpoint that triggers job
- File: Watch directory for file creation
- Queue: (future) consume from Redis/RabbitMQ

### Files
- `src/procclaw/core/triggers.py` - Trigger handlers
- Update `models.py` - TriggerConfig
- Update `api/server.py` - webhook endpoints
- Update `supervisor.py` - trigger registration

### Webhook API
```
POST /api/v1/trigger/{job_id}
Body: { "payload": {...}, "idempotency_key": "..." }
```

---

## 6. Deduplication

### Design
- Dedup window: don't run same job within X seconds
- Idempotency keys for webhook triggers
- Run fingerprint based on job_id + params
- Configurable: `dedup.window_seconds`, `dedup.enabled`

### Files
- `src/procclaw/core/dedup.py` - Deduplication manager
- Update `models.py` - DedupConfig
- Update `db.py` - recent runs tracking
- Update `supervisor.py` - dedup check before start

---

## Implementation Order

1. **Deduplication** - Foundation for others
2. **Max Concurrent** - Basic concurrency control
3. **Priority Queues** - Ordering improvements
4. **Dead-Letter Queue** - Failure handling
5. **Distributed Locks** - Multi-instance safety
6. **Event Triggers** - External integration

---

## Testing Strategy

### Unit Tests
- Each new module gets comprehensive tests
- Edge cases: timeouts, failures, race conditions

### Integration Tests
- Full flow: trigger → queue → execute → retry → DLQ
- Concurrent execution scenarios
- Lock contention scenarios

### Stress Tests
- High concurrency (100+ simultaneous jobs)
- Lock contention with multiple instances
- DLQ with thousands of failed jobs

---

## Database Migrations

Version 2 schema additions:
- `dead_letter_jobs` table
- `job_configs.priority` column
- `job_configs.max_concurrent` column
- `job_configs.dedup_window` column
- `recent_executions` table for dedup
