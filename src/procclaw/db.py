"""SQLite database for ProcClaw state and history."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator

from loguru import logger

from procclaw.config import DEFAULT_DB_FILE
from procclaw.models import JobRun, JobState, JobStatus

# Schema version for migrations
SCHEMA_VERSION = 10

SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- Jobs state
CREATE TABLE IF NOT EXISTS job_state (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'stopped',
    pid INTEGER,
    started_at TEXT,
    stopped_at TEXT,
    restart_count INTEGER DEFAULT 0,
    retry_attempt INTEGER DEFAULT 0,
    last_exit_code INTEGER,
    last_error TEXT,
    next_run TEXT,
    next_retry TEXT
);

-- Run history
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    duration_seconds REAL,
    trigger TEXT DEFAULT 'manual',
    error TEXT,
    fingerprint TEXT,
    idempotency_key TEXT,
    composite_id TEXT,
    session_key TEXT,
    session_transcript TEXT,
    session_messages TEXT,
    sla_snapshot_id INTEGER,
    sla_status TEXT,
    sla_details TEXT,
    healing_status TEXT,
    healing_attempts INTEGER DEFAULT 0,
    healing_session_key TEXT,
    healing_result TEXT,
    original_exit_code INTEGER
);

-- Metrics (for historical queries)
CREATE TABLE IF NOT EXISTS job_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL
);

-- Dead Letter Queue
CREATE TABLE IF NOT EXISTS dead_letter_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    original_run_id INTEGER,
    failed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    job_config TEXT,
    trigger_params TEXT,
    reinjected_at TEXT,
    reinjected_run_id INTEGER
);

-- Execution records for deduplication
CREATE TABLE IF NOT EXISTS execution_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    idempotency_key TEXT,
    started_at TEXT NOT NULL,
    UNIQUE(fingerprint, started_at)
);

-- Distributed locks
CREATE TABLE IF NOT EXISTS job_locks (
    job_id TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Priority queue (for pending jobs)
CREATE TABLE IF NOT EXISTS job_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    priority INTEGER DEFAULT 2,
    queued_at TEXT NOT NULL,
    trigger TEXT DEFAULT 'manual',
    params TEXT,
    idempotency_key TEXT,
    status TEXT DEFAULT 'pending'
);

-- ETA scheduling
CREATE TABLE IF NOT EXISTS eta_jobs (
    job_id TEXT PRIMARY KEY,
    run_at TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    trigger TEXT DEFAULT 'eta',
    params TEXT,
    idempotency_key TEXT,
    one_shot INTEGER DEFAULT 1,
    triggered INTEGER DEFAULT 0,
    triggered_at TEXT
);

-- Revocations
CREATE TABLE IF NOT EXISTS revocations (
    job_id TEXT PRIMARY KEY,
    revoked_at TEXT NOT NULL,
    reason TEXT,
    expires_at TEXT,
    terminate INTEGER DEFAULT 0
);

-- Workflows
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    config TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Workflow runs
CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    config TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    current_step INTEGER DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    job_run_ids TEXT,
    results TEXT,
    error TEXT,
    FOREIGN KEY (workflow_id) REFERENCES workflows(id)
);

-- Job results
CREATE TABLE IF NOT EXISTS job_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    exit_code INTEGER,
    stdout_tail TEXT,
    stderr_tail TEXT,
    output_data TEXT,
    duration_seconds REAL,
    finished_at TEXT NOT NULL,
    workflow_run_id INTEGER,
    step_index INTEGER,
    FOREIGN KEY (run_id) REFERENCES job_runs(id),
    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id)
);

-- SLA Snapshots (job config versions for SLA comparison)
CREATE TABLE IF NOT EXISTS job_sla_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    sla_json TEXT,
    UNIQUE(job_id, config_hash)
);

-- SLA Metrics (aggregated metrics by period)
CREATE TABLE IF NOT EXISTS sla_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_type TEXT NOT NULL,
    total_runs INTEGER DEFAULT 0,
    successful_runs INTEGER DEFAULT 0,
    failed_runs INTEGER DEFAULT 0,
    late_starts INTEGER DEFAULT 0,
    over_duration INTEGER DEFAULT 0,
    success_rate REAL,
    schedule_adherence REAL,
    duration_compliance REAL,
    availability REAL,
    avg_duration REAL,
    p50_duration REAL,
    p95_duration REAL,
    max_duration REAL,
    details_json TEXT,
    UNIQUE(job_id, period_start, period_type)
);

-- Job logs (per-run logs stored in SQLite)
CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    level TEXT DEFAULT 'stdout',  -- stdout, stderr
    line TEXT NOT NULL,
    line_num INTEGER,
    FOREIGN KEY (run_id) REFERENCES job_runs(id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_job_logs_run_id ON job_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id);
CREATE INDEX IF NOT EXISTS idx_job_logs_timestamp ON job_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_job_runs_job_id ON job_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_job_runs_started_at ON job_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_job_metrics_job_id ON job_metrics(job_id);
CREATE INDEX IF NOT EXISTS idx_job_metrics_timestamp ON job_metrics(timestamp);
CREATE INDEX IF NOT EXISTS idx_dead_letter_job_id ON dead_letter_jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_dead_letter_failed_at ON dead_letter_jobs(failed_at);
CREATE INDEX IF NOT EXISTS idx_execution_records_fingerprint ON execution_records(fingerprint);
CREATE INDEX IF NOT EXISTS idx_execution_records_started_at ON execution_records(started_at);
CREATE INDEX IF NOT EXISTS idx_job_queue_priority ON job_queue(priority, queued_at);
CREATE INDEX IF NOT EXISTS idx_job_queue_status ON job_queue(status);
CREATE INDEX IF NOT EXISTS idx_eta_jobs_run_at ON eta_jobs(run_at);
CREATE INDEX IF NOT EXISTS idx_revocations_expires_at ON revocations(expires_at);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_id ON workflow_runs(workflow_id);
CREATE INDEX IF NOT EXISTS idx_job_results_job_id ON job_results(job_id);
CREATE INDEX IF NOT EXISTS idx_job_results_workflow_run_id ON job_results(workflow_run_id);
CREATE INDEX IF NOT EXISTS idx_sla_snapshots_job_id ON job_sla_snapshots(job_id);
CREATE INDEX IF NOT EXISTS idx_sla_metrics_job_id ON sla_metrics(job_id, period_type);
"""


class Database:
    """SQLite database manager for ProcClaw."""

    def __init__(self, db_path: Path | None = None):
        """Initialize the database."""
        self.db_path = db_path or DEFAULT_DB_FILE
        self._ensure_db()

    def _ensure_db(self) -> None:
        """Ensure the database exists and is up to date."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with self._connect() as conn:
                # Create tables
                conn.executescript(SCHEMA_SQL)

                # Check/update schema version
                cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
                row = cursor.fetchone()

                if row is None:
                    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
                    logger.debug(f"Initialized database at {self.db_path}")
                elif row[0] < SCHEMA_VERSION:
                    # Run migrations if needed
                    self._migrate(conn, row[0], SCHEMA_VERSION)
        except sqlite3.DatabaseError as e:
            if "malformed" in str(e).lower() or "corrupt" in str(e).lower():
                logger.error(f"Database corruption detected at {self.db_path}: {e}")
                # Backup corrupted file and create fresh database
                backup_path = self.db_path.with_suffix(".db.corrupt")
                try:
                    import shutil
                    shutil.move(str(self.db_path), str(backup_path))
                    logger.warning(f"Moved corrupted database to {backup_path}")
                except Exception as move_error:
                    logger.error(f"Failed to backup corrupted database: {move_error}")
                    # Try to delete instead
                    try:
                        self.db_path.unlink()
                    except:
                        pass
                
                # Retry with fresh database
                with self._connect() as conn:
                    conn.executescript(SCHEMA_SQL)
                    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
                    logger.info(f"Created fresh database at {self.db_path}")
            else:
                raise

    def _migrate(self, conn: sqlite3.Connection, from_version: int, to_version: int) -> None:
        """Run database migrations."""
        logger.info(f"Migrating database from version {from_version} to {to_version}")
        
        if from_version < 2 and to_version >= 2:
            # Migration to version 2: Add new tables
            logger.info("Migrating to version 2: Adding DLQ, dedup, locks, queue tables")
            
            # Add new columns to job_runs
            try:
                conn.execute("ALTER TABLE job_runs ADD COLUMN fingerprint TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
            
            try:
                conn.execute("ALTER TABLE job_runs ADD COLUMN idempotency_key TEXT")
            except sqlite3.OperationalError:
                pass
            
            # Create new tables (idempotent)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS dead_letter_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    original_run_id INTEGER,
                    failed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    job_config TEXT,
                    trigger_params TEXT,
                    reinjected_at TEXT,
                    reinjected_run_id INTEGER
                );
                
                CREATE TABLE IF NOT EXISTS execution_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    idempotency_key TEXT,
                    started_at TEXT NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS job_locks (
                    job_id TEXT PRIMARY KEY,
                    holder_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS job_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    priority INTEGER DEFAULT 2,
                    queued_at TEXT NOT NULL,
                    trigger TEXT DEFAULT 'manual',
                    params TEXT,
                    idempotency_key TEXT,
                    status TEXT DEFAULT 'pending'
                );
                
                CREATE INDEX IF NOT EXISTS idx_dead_letter_job_id ON dead_letter_jobs(job_id);
                CREATE INDEX IF NOT EXISTS idx_execution_records_fingerprint ON execution_records(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_job_queue_priority ON job_queue(priority, queued_at);
            """)
        
        if from_version < 5 and to_version >= 5:
            # Migration to version 5: Add composite_id to track workflow runs
            logger.info("Migrating to version 5: Adding composite_id to job_runs")
            try:
                conn.execute("ALTER TABLE job_runs ADD COLUMN composite_id TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
        
        if from_version < 6 and to_version >= 6:
            # Migration to version 6: Add session_key and session_transcript for OpenClaw integration
            logger.info("Migrating to version 6: Adding session_key and session_transcript to job_runs")
            try:
                conn.execute("ALTER TABLE job_runs ADD COLUMN session_key TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE job_runs ADD COLUMN session_transcript TEXT")
            except sqlite3.OperationalError:
                pass
        
        if from_version < 7 and to_version >= 7:
            # Migration to version 7: Add self-healing fields to job_runs
            logger.info("Migrating to version 7: Adding self-healing fields to job_runs")
            healing_columns = [
                ("healing_status", "TEXT"),
                ("healing_attempts", "INTEGER DEFAULT 0"),
                ("healing_session_key", "TEXT"),
                ("healing_result", "TEXT"),
                ("original_exit_code", "INTEGER"),
            ]
            for col_name, col_type in healing_columns:
                try:
                    conn.execute(f"ALTER TABLE job_runs ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
        
        if from_version < 8 and to_version >= 8:
            # Migration to version 8: Add paused field to job_state
            logger.info("Migrating to version 8: Adding paused field to job_state")
            try:
                conn.execute("ALTER TABLE job_state ADD COLUMN paused INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
        
        if from_version < 9 and to_version >= 9:
            # Migration to version 9: Add session_messages to store AI session content in DB
            # This makes session data persistent and independent of OpenClaw JSONL files
            logger.info("Migrating to version 9: Adding session_messages to job_runs")
            try:
                conn.execute("ALTER TABLE job_runs ADD COLUMN session_messages TEXT")
            except sqlite3.OperationalError:
                pass
        
        if from_version < 10 and to_version >= 10:
            # Migration to version 10: Add SLA tracking
            logger.info("Migrating to version 10: Adding SLA tables and columns")
            
            # Create SLA snapshots table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS job_sla_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    snapshot_at TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    sla_json TEXT,
                    UNIQUE(job_id, config_hash)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sla_snapshots_job_id ON job_sla_snapshots(job_id)")
            
            # Create SLA metrics table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sla_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    period_type TEXT NOT NULL,
                    total_runs INTEGER DEFAULT 0,
                    successful_runs INTEGER DEFAULT 0,
                    failed_runs INTEGER DEFAULT 0,
                    late_starts INTEGER DEFAULT 0,
                    over_duration INTEGER DEFAULT 0,
                    success_rate REAL,
                    schedule_adherence REAL,
                    duration_compliance REAL,
                    availability REAL,
                    avg_duration REAL,
                    p50_duration REAL,
                    p95_duration REAL,
                    max_duration REAL,
                    details_json TEXT,
                    UNIQUE(job_id, period_start, period_type)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sla_metrics_job_id ON sla_metrics(job_id, period_type)")
            
            # Add SLA columns to job_runs
            sla_columns = [
                ("sla_snapshot_id", "INTEGER"),
                ("sla_status", "TEXT"),
                ("sla_details", "TEXT"),
            ]
            for col_name, col_type in sla_columns:
                try:
                    conn.execute(f"ALTER TABLE job_runs ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
        
        conn.execute("UPDATE schema_version SET version = ?", (to_version,))

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Job State Methods

    def get_state(self, job_id: str) -> JobState | None:
        """Get the current state of a job."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM job_state WHERE job_id = ?", (job_id,)
            )
            row = cursor.fetchone()

            if row is None:
                return None

            return self._row_to_state(row)

    def get_all_states(self) -> dict[str, JobState]:
        """Get all job states."""
        with self._connect() as conn:
            cursor = conn.execute("SELECT * FROM job_state")
            rows = cursor.fetchall()

            return {row["job_id"]: self._row_to_state(row) for row in rows}

    def save_state(self, state: JobState) -> None:
        """Save or update a job state."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO job_state (
                    job_id, status, pid, started_at, stopped_at,
                    restart_count, retry_attempt, last_exit_code, last_error,
                    next_run, next_retry, paused
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.job_id,
                    state.status.value,
                    state.pid,
                    state.started_at.isoformat() if state.started_at else None,
                    state.stopped_at.isoformat() if state.stopped_at else None,
                    state.restart_count,
                    state.retry_attempt,
                    state.last_exit_code,
                    state.last_error,
                    state.next_run.isoformat() if state.next_run else None,
                    state.next_retry.isoformat() if state.next_retry else None,
                    1 if state.paused else 0,
                ),
            )

    def delete_state(self, job_id: str) -> None:
        """Delete a job state."""
        with self._connect() as conn:
            conn.execute("DELETE FROM job_state WHERE job_id = ?", (job_id,))

    def _row_to_state(self, row: sqlite3.Row) -> JobState:
        """Convert a database row to a JobState."""
        # Handle paused column (may not exist in older databases)
        paused = False
        try:
            paused = bool(row["paused"])
        except (IndexError, KeyError):
            pass
        
        return JobState(
            job_id=row["job_id"],
            status=JobStatus(row["status"]),
            pid=row["pid"],
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            stopped_at=datetime.fromisoformat(row["stopped_at"]) if row["stopped_at"] else None,
            restart_count=row["restart_count"] or 0,
            retry_attempt=row["retry_attempt"] or 0,
            last_exit_code=row["last_exit_code"],
            last_error=row["last_error"],
            next_run=datetime.fromisoformat(row["next_run"]) if row["next_run"] else None,
            next_retry=datetime.fromisoformat(row["next_retry"]) if row["next_retry"] else None,
            paused=paused,
        )

    # Job Runs Methods

    def add_run(self, run: JobRun) -> int:
        """Add a job run record."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO job_runs (
                    job_id, started_at, finished_at, exit_code,
                    duration_seconds, trigger, error, composite_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.job_id,
                    run.started_at.isoformat(),
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.exit_code,
                    run.duration_seconds,
                    run.trigger,
                    run.error,
                    run.composite_id,
                ),
            )
            return cursor.lastrowid or 0

    def update_run(self, run: JobRun) -> None:
        """Update a job run record."""
        import json
        
        if run.id is None:
            raise ValueError("Cannot update run without ID")
        
        # Serialize healing_result to JSON if present
        healing_result_json = None
        if run.healing_result:
            healing_result_json = json.dumps(run.healing_result)

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE job_runs SET
                    finished_at = ?,
                    exit_code = ?,
                    duration_seconds = ?,
                    error = ?,
                    session_key = ?,
                    session_transcript = ?,
                    session_messages = ?,
                    sla_snapshot_id = ?,
                    sla_status = ?,
                    sla_details = ?,
                    healing_status = ?,
                    healing_attempts = ?,
                    healing_session_key = ?,
                    healing_result = ?,
                    original_exit_code = ?
                WHERE id = ?
                """,
                (
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.exit_code,
                    run.duration_seconds,
                    run.error,
                    run.session_key,
                    run.session_transcript,
                    run.session_messages,
                    run.sla_snapshot_id,
                    run.sla_status,
                    run.sla_details,
                    run.healing_status,
                    run.healing_attempts,
                    run.healing_session_key,
                    healing_result_json,
                    run.original_exit_code,
                    run.id,
                ),
            )

    def get_runs(
        self,
        job_id: str | None = None,
        limit: int = 100,
        since: datetime | None = None,
    ) -> list[JobRun]:
        """Get job run history."""
        with self._connect() as conn:
            query = "SELECT * FROM job_runs WHERE 1=1"
            params: list = []

            if job_id:
                query += " AND job_id = ?"
                params.append(job_id)

            if since:
                query += " AND started_at >= ?"
                params.append(since.isoformat())

            query += " ORDER BY started_at DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [self._row_to_run(row) for row in rows]

    def get_last_run(self, job_id: str) -> JobRun | None:
        """Get the last run of a job."""
        runs = self.get_runs(job_id=job_id, limit=1)
        return runs[0] if runs else None

    def get_running_runs(self) -> list[JobRun]:
        """Get all runs that are marked as running (no finished_at)."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM job_runs WHERE finished_at IS NULL ORDER BY started_at DESC"
            )
            rows = cursor.fetchall()
            return [self._row_to_run(row) for row in rows]

    def _row_to_run(self, row: sqlite3.Row) -> JobRun:
        """Convert a database row to a JobRun."""
        import json
        keys = row.keys()
        
        # Parse healing_result JSON if present
        healing_result = None
        if "healing_result" in keys and row["healing_result"]:
            try:
                healing_result = json.loads(row["healing_result"])
            except (json.JSONDecodeError, TypeError):
                healing_result = None
        
        return JobRun(
            id=row["id"],
            job_id=row["job_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            exit_code=row["exit_code"],
            duration_seconds=row["duration_seconds"],
            trigger=row["trigger"],
            error=row["error"],
            composite_id=row["composite_id"] if "composite_id" in keys else None,
            session_key=row["session_key"] if "session_key" in keys else None,
            session_transcript=row["session_transcript"] if "session_transcript" in keys else None,
            session_messages=row["session_messages"] if "session_messages" in keys else None,
            sla_snapshot_id=row["sla_snapshot_id"] if "sla_snapshot_id" in keys else None,
            sla_status=row["sla_status"] if "sla_status" in keys else None,
            sla_details=row["sla_details"] if "sla_details" in keys else None,
            healing_status=row["healing_status"] if "healing_status" in keys else None,
            healing_attempts=row["healing_attempts"] if "healing_attempts" in keys else 0,
            healing_session_key=row["healing_session_key"] if "healing_session_key" in keys else None,
            healing_result=healing_result,
            original_exit_code=row["original_exit_code"] if "original_exit_code" in keys else None,
        )

    # Log Methods

    def add_log_line(
        self,
        run_id: int,
        job_id: str,
        line: str,
        level: str = "stdout",
        timestamp: datetime | None = None,
        line_num: int | None = None,
    ) -> None:
        """Add a log line for a job run."""
        if timestamp is None:
            timestamp = datetime.now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO job_logs (run_id, job_id, timestamp, level, line, line_num)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, job_id, timestamp.isoformat(), level, line, line_num),
            )

    def add_log_lines(
        self,
        run_id: int,
        job_id: str,
        lines: list[str],
        level: str = "stdout",
        timestamp: datetime | None = None,
    ) -> None:
        """Add multiple log lines for a job run (batch insert)."""
        if timestamp is None:
            timestamp = datetime.now()
        ts = timestamp.isoformat()
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO job_logs (run_id, job_id, timestamp, level, line, line_num)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(run_id, job_id, ts, level, line, i) for i, line in enumerate(lines)],
            )

    def get_logs(
        self,
        run_id: int | None = None,
        job_id: str | None = None,
        level: str | None = None,
        limit: int = 1000,
        since: datetime | None = None,
    ) -> list[dict]:
        """Get log lines, optionally filtered by run_id, job_id, or level."""
        with self._connect() as conn:
            query = "SELECT * FROM job_logs WHERE 1=1"
            params: list = []

            if run_id is not None:
                query += " AND run_id = ?"
                params.append(run_id)

            if job_id is not None:
                query += " AND job_id = ?"
                params.append(job_id)

            if level is not None:
                query += " AND level = ?"
                params.append(level)

            if since is not None:
                query += " AND timestamp >= ?"
                params.append(since.isoformat())

            query += " ORDER BY id ASC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [
                {
                    "id": row["id"],
                    "run_id": row["run_id"],
                    "job_id": row["job_id"],
                    "timestamp": row["timestamp"],
                    "level": row["level"],
                    "line": row["line"],
                    "line_num": row["line_num"],
                }
                for row in rows
            ]

    def delete_logs(self, run_id: int) -> int:
        """Delete all log lines for a run.
        
        Returns the number of deleted rows.
        """
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM job_logs WHERE run_id = ?", (run_id,))
            return cursor.rowcount

    def delete_run(self, run_id: int) -> bool:
        """Delete a job run.
        
        Returns True if a row was deleted.
        """
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM job_runs WHERE id = ?", (run_id,))
            return cursor.rowcount > 0

    def get_run_logs(self, run_id: int, level: str | None = None, limit: int = 5000) -> list[str]:
        """Get log lines for a specific run as a simple list of strings."""
        logs = self.get_logs(run_id=run_id, level=level, limit=limit)
        return [log["line"] for log in logs]

    def cleanup_old_logs(self, days: int = 30) -> int:
        """Remove logs older than specified days."""
        cutoff = datetime.now() - timedelta(days=days)
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM job_logs WHERE timestamp < ?",
                (cutoff.isoformat(),),
            )
            deleted = cursor.rowcount
            logger.info(f"Cleaned up {deleted} old log entries")
            return deleted

    # Metrics Methods

    def add_metric(self, job_id: str, name: str, value: float) -> None:
        """Add a metric data point."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO job_metrics (job_id, timestamp, metric_name, metric_value)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, datetime.now().isoformat(), name, value),
            )

    def get_metrics(
        self,
        job_id: str,
        metric_name: str | None = None,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Get metrics for a job."""
        with self._connect() as conn:
            query = "SELECT * FROM job_metrics WHERE job_id = ?"
            params: list = [job_id]

            if metric_name:
                query += " AND metric_name = ?"
                params.append(metric_name)

            if since:
                query += " AND timestamp >= ?"
                params.append(since.isoformat())

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [
                {
                    "timestamp": row["timestamp"],
                    "name": row["metric_name"],
                    "value": row["metric_value"],
                }
                for row in rows
            ]

    # Statistics Methods

    def get_job_stats(self, job_id: str) -> dict:
        """Get statistics for a job."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT
                    COUNT(*) as total_runs,
                    SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END) as successful_runs,
                    SUM(CASE WHEN exit_code != 0 THEN 1 ELSE 0 END) as failed_runs,
                    AVG(duration_seconds) as avg_duration,
                    MAX(duration_seconds) as max_duration,
                    MIN(duration_seconds) as min_duration
                FROM job_runs
                WHERE job_id = ? AND finished_at IS NOT NULL
                """,
                (job_id,),
            )
            row = cursor.fetchone()

            return {
                "total_runs": row["total_runs"] or 0,
                "successful_runs": row["successful_runs"] or 0,
                "failed_runs": row["failed_runs"] or 0,
                "avg_duration": row["avg_duration"],
                "max_duration": row["max_duration"],
                "min_duration": row["min_duration"],
            }

    # Cleanup Methods

    def cleanup_old_runs(self, days: int = 30) -> int:
        """Delete run records older than specified days."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM job_runs
                WHERE started_at < datetime('now', ?)
                """,
                (f"-{days} days",),
            )
            deleted = cursor.rowcount
            logger.info(f"Cleaned up {deleted} old run records")
            return deleted

    def cleanup_old_metrics(self, days: int = 7) -> int:
        """Delete metric records older than specified days."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM job_metrics
                WHERE timestamp < datetime('now', ?)
                """,
                (f"-{days} days",),
            )
            deleted = cursor.rowcount
            logger.info(f"Cleaned up {deleted} old metric records")
            return deleted

    # =========================================================================
    # Dead Letter Queue Methods
    # =========================================================================

    def add_to_dlq(
        self,
        job_id: str,
        run_id: int | None,
        error: str,
        attempts: int,
        job_config: str | None = None,
        trigger_params: str | None = None,
    ) -> int:
        """Add a failed job to the dead letter queue."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO dead_letter_jobs 
                (job_id, original_run_id, last_error, attempts, job_config, trigger_params)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, run_id, error, attempts, job_config, trigger_params),
            )
            dlq_id = cursor.lastrowid
            logger.info(f"Added job '{job_id}' to DLQ (id={dlq_id})")
            return dlq_id

    def get_dlq_entries(
        self,
        job_id: str | None = None,
        include_reinjected: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """Get entries from the dead letter queue."""
        with self._connect() as conn:
            query = "SELECT * FROM dead_letter_jobs WHERE 1=1"
            params: list = []

            if job_id:
                query += " AND job_id = ?"
                params.append(job_id)

            if not include_reinjected:
                query += " AND reinjected_at IS NULL"

            query += " ORDER BY failed_at DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [dict(row) for row in rows]

    def reinject_from_dlq(self, dlq_id: int, new_run_id: int) -> bool:
        """Mark a DLQ entry as reinjected."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE dead_letter_jobs
                SET reinjected_at = ?, reinjected_run_id = ?
                WHERE id = ? AND reinjected_at IS NULL
                """,
                (datetime.now().isoformat(), new_run_id, dlq_id),
            )
            return cursor.rowcount > 0

    def delete_dlq_entry(self, dlq_id: int) -> bool:
        """Delete a DLQ entry."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM dead_letter_jobs WHERE id = ?",
                (dlq_id,),
            )
            return cursor.rowcount > 0

    def cleanup_old_dlq(self, days: int = 30) -> int:
        """Delete old DLQ entries."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM dead_letter_jobs
                WHERE failed_at < datetime('now', ?)
                """,
                (f"-{days} days",),
            )
            return cursor.rowcount

    # =========================================================================
    # Deduplication Methods
    # =========================================================================

    def record_execution(
        self,
        job_id: str,
        run_id: int,
        fingerprint: str,
        idempotency_key: str | None = None,
    ) -> None:
        """Record an execution for deduplication."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO execution_records
                (job_id, run_id, fingerprint, idempotency_key, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, run_id, fingerprint, idempotency_key, datetime.now().isoformat()),
            )

    def get_recent_executions(
        self,
        since: datetime,
        job_id: str | None = None,
    ) -> list:
        """Get recent executions for deduplication cache."""
        with self._connect() as conn:
            query = "SELECT * FROM execution_records WHERE started_at >= ?"
            params: list = [since.isoformat()]

            if job_id:
                query += " AND job_id = ?"
                params.append(job_id)

            cursor = conn.execute(query, params)
            return cursor.fetchall()

    def cleanup_old_execution_records(self, hours: int = 24) -> int:
        """Delete old execution records."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM execution_records
                WHERE started_at < datetime('now', ?)
                """,
                (f"-{hours} hours",),
            )
            return cursor.rowcount

    # =========================================================================
    # Distributed Lock Methods
    # =========================================================================

    def acquire_lock(
        self,
        job_id: str,
        holder_id: str,
        timeout_seconds: int,
    ) -> bool:
        """Try to acquire a lock for a job."""
        now = datetime.now()
        expires_at = now + timedelta(seconds=timeout_seconds)
        
        with self._connect() as conn:
            # First, clean up expired locks
            conn.execute(
                "DELETE FROM job_locks WHERE expires_at < ?",
                (now.isoformat(),),
            )

            # Try to acquire
            try:
                conn.execute(
                    """
                    INSERT INTO job_locks (job_id, holder_id, acquired_at, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_id, holder_id, now.isoformat(), expires_at.isoformat()),
                )
                return True
            except sqlite3.IntegrityError:
                # Lock already held
                return False

    def release_lock(self, job_id: str, holder_id: str) -> bool:
        """Release a lock (only if we hold it)."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM job_locks
                WHERE job_id = ? AND holder_id = ?
                """,
                (job_id, holder_id),
            )
            return cursor.rowcount > 0

    def is_locked(self, job_id: str) -> bool:
        """Check if a job is locked."""
        with self._connect() as conn:
            # Clean expired first
            conn.execute(
                "DELETE FROM job_locks WHERE expires_at < ?",
                (datetime.now().isoformat(),),
            )

            cursor = conn.execute(
                "SELECT 1 FROM job_locks WHERE job_id = ?",
                (job_id,),
            )
            return cursor.fetchone() is not None

    def get_lock_holder(self, job_id: str) -> str | None:
        """Get the current lock holder for a job."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT holder_id FROM job_locks WHERE job_id = ? AND expires_at > ?",
                (job_id, datetime.now().isoformat()),
            )
            row = cursor.fetchone()
            return row["holder_id"] if row else None

    # =========================================================================
    # Priority Queue Methods
    # =========================================================================

    def enqueue_job(
        self,
        job_id: str,
        priority: int = 2,
        trigger: str = "manual",
        params: str | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        """Add a job to the priority queue."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO job_queue 
                (job_id, priority, queued_at, trigger, params, idempotency_key, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (job_id, priority, datetime.now().isoformat(), trigger, params, idempotency_key),
            )
            return cursor.lastrowid

    def dequeue_next(self) -> dict | None:
        """Get and claim the next job from the queue."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM job_queue
                WHERE status = 'pending'
                ORDER BY priority ASC, queued_at ASC
                LIMIT 1
                """
            )
            row = cursor.fetchone()

            if row is None:
                return None

            # Mark as claimed
            conn.execute(
                "UPDATE job_queue SET status = 'claimed' WHERE id = ?",
                (row["id"],),
            )

            return dict(row)

    def complete_queued_job(self, queue_id: int) -> None:
        """Mark a queued job as completed."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE job_queue SET status = 'completed' WHERE id = ?",
                (queue_id,),
            )

    def fail_queued_job(self, queue_id: int) -> None:
        """Mark a queued job as failed."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE job_queue SET status = 'failed' WHERE id = ?",
                (queue_id,),
            )

    def get_queue_length(self, job_id: str | None = None) -> int:
        """Get the number of pending jobs in the queue."""
        with self._connect() as conn:
            query = "SELECT COUNT(*) FROM job_queue WHERE status = 'pending'"
            params: list = []

            if job_id:
                query += " AND job_id = ?"
                params.append(job_id)

            cursor = conn.execute(query, params)
            return cursor.fetchone()[0]

    def cleanup_old_queue_entries(self, hours: int = 24) -> int:
        """Delete old completed/failed queue entries."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM job_queue
                WHERE status IN ('completed', 'failed')
                AND queued_at < datetime('now', ?)
                """,
                (f"-{hours} hours",),
            )
            return cursor.rowcount

    # =========================================================================
    # Generic Methods (for new modules)
    # =========================================================================

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a SELECT query and return results as dicts.
        
        Args:
            sql: SQL query string
            params: Query parameters
            
        Returns:
            List of row dicts
        """
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Execute an INSERT/UPDATE/DELETE statement.
        
        Args:
            sql: SQL statement
            params: Statement parameters
            
        Returns:
            Number of rows affected or last row ID
        """
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            # Return lastrowid for INSERTs, rowcount for others
            if sql.strip().upper().startswith("INSERT"):
                return cursor.lastrowid or 0
            return cursor.rowcount
