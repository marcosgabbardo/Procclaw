"""SQLite database for ProcClaw state and history."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from loguru import logger

from procclaw.config import DEFAULT_DB_FILE
from procclaw.models import JobRun, JobState, JobStatus

# Schema version for migrations
SCHEMA_VERSION = 1

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
    error TEXT
);

-- Metrics (for historical queries)
CREATE TABLE IF NOT EXISTS job_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_job_runs_job_id ON job_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_job_runs_started_at ON job_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_job_metrics_job_id ON job_metrics(job_id);
CREATE INDEX IF NOT EXISTS idx_job_metrics_timestamp ON job_metrics(timestamp);
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

    def _migrate(self, conn: sqlite3.Connection, from_version: int, to_version: int) -> None:
        """Run database migrations."""
        logger.info(f"Migrating database from version {from_version} to {to_version}")
        # Add migration logic here as needed
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
                    next_run, next_retry
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )

    def delete_state(self, job_id: str) -> None:
        """Delete a job state."""
        with self._connect() as conn:
            conn.execute("DELETE FROM job_state WHERE job_id = ?", (job_id,))

    def _row_to_state(self, row: sqlite3.Row) -> JobState:
        """Convert a database row to a JobState."""
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
        )

    # Job Runs Methods

    def add_run(self, run: JobRun) -> int:
        """Add a job run record."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO job_runs (
                    job_id, started_at, finished_at, exit_code,
                    duration_seconds, trigger, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.job_id,
                    run.started_at.isoformat(),
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.exit_code,
                    run.duration_seconds,
                    run.trigger,
                    run.error,
                ),
            )
            return cursor.lastrowid or 0

    def update_run(self, run: JobRun) -> None:
        """Update a job run record."""
        if run.id is None:
            raise ValueError("Cannot update run without ID")

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE job_runs SET
                    finished_at = ?,
                    exit_code = ?,
                    duration_seconds = ?,
                    error = ?
                WHERE id = ?
                """,
                (
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.exit_code,
                    run.duration_seconds,
                    run.error,
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

    def _row_to_run(self, row: sqlite3.Row) -> JobRun:
        """Convert a database row to a JobRun."""
        return JobRun(
            id=row["id"],
            job_id=row["job_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            exit_code=row["exit_code"],
            duration_seconds=row["duration_seconds"],
            trigger=row["trigger"],
            error=row["error"],
        )

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
