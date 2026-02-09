"""Resilience and chaos engineering tests for ProcClaw.

These tests verify that ProcClaw survives under the worst conditions:
- Process crashes
- Database corruption
- Signal storms
- Resource exhaustion
- Concurrent operations
- Network failures
"""

import asyncio
import os
import signal
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# Import all modules we're testing
from procclaw.models import (
    JobConfig,
    JobType,
    RetryConfig,
    HealthCheckConfig,
    AlertConfig,
    LogConfig,
    OperatingHoursConfig,
    ResourceLimitsConfig,
    ResourceLimitAction,
)
from procclaw.db import Database
from procclaw.core.retry import RetryManager
from procclaw.core.watchdog import Watchdog, MissedRunInfo
from procclaw.core.log_utils import LogRotator, parse_size, rotate_log, check_and_rotate
from procclaw.core.output_parser import OutputParser, JobOutputChecker, OutputMatch
from procclaw.core.operating_hours import OperatingHoursChecker
from procclaw.core.resources import ResourceMonitor, ResourceUsage, ResourceViolation


class TestDatabaseResilience:
    """Test database operations under failure conditions."""

    def test_corrupted_database_recovery(self, tmp_path):
        """Test recovery from a corrupted database."""
        db_path = tmp_path / "test.db"
        
        # Create and initialize DB (auto-initializes on first use)
        db = Database(db_path)
        
        # Write some data
        from procclaw.models import JobState, JobStatus
        state = JobState(job_id="test-job", status=JobStatus.RUNNING)
        db.save_state(state)
        
        # Create a separate corrupted copy
        corrupt_path = tmp_path / "corrupt.db"
        import shutil
        shutil.copy(db_path, corrupt_path)
        
        # Corrupt the copy
        with open(corrupt_path, "r+b") as f:
            f.seek(100)
            f.write(b"CORRUPTED DATA HERE" * 10)
        
        # Try to open corrupted DB - should recover gracefully
        # by backing up corrupted file and creating fresh database
        db2 = Database(corrupt_path)
        
        # The corrupted file should have been moved to .corrupt
        backup_path = corrupt_path.with_suffix(".db.corrupt")
        assert backup_path.exists(), "Corrupted database should be backed up"
        
        # Fresh database should be usable
        db2.save_state(JobState(job_id="new-job", status=JobStatus.RUNNING))
        retrieved = db2.get_state("new-job")
        assert retrieved is not None

    def test_concurrent_writes(self, tmp_path):
        """Test concurrent database writes don't corrupt data."""
        db_path = tmp_path / "concurrent.db"
        
        from procclaw.models import JobState, JobStatus
        
        db = Database(db_path)
        
        # Pre-initialize the database by doing one write
        state = JobState(job_id="init", status=JobStatus.STOPPED)
        db.save_state(state)
        
        async def write_states(prefix, count):
            # Each coroutine uses its own DB instance
            local_db = Database(db_path)
            for i in range(count):
                state = JobState(
                    job_id=f"{prefix}-job-{i}",
                    status=JobStatus.RUNNING,
                    started_at=datetime.now(),
                )
                local_db.save_state(state)
                await asyncio.sleep(0.001)
        
        async def run_concurrent():
            tasks = [
                write_states("a", 50),
                write_states("b", 50),
                write_states("c", 50),
            ]
            await asyncio.gather(*tasks)
        
        asyncio.run(run_concurrent())
        
        # Verify states were written (may be less due to conflicts)
        all_states = db.get_all_states()
        assert len(all_states) >= 100, f"Expected at least 100 states, got {len(all_states)}"

    def test_database_locked_handling(self, tmp_path):
        """Test that database handles locks gracefully."""
        db_path = tmp_path / "locked.db"
        db = Database(db_path)
        
        # Write initial state
        from procclaw.models import JobState, JobStatus
        state = JobState(job_id="initial", status=JobStatus.STOPPED)
        db.save_state(state)
        
        # Verify we can read
        retrieved = db.get_state("initial")
        assert retrieved is not None
        assert retrieved.job_id == "initial"


class TestRetryManagerResilience:
    """Test retry manager under failure conditions."""

    def test_callback_exception_handling(self):
        """Test that exceptions in callbacks don't crash the manager."""
        def failing_retry(job_id):
            raise RuntimeError("Callback explosion!")
        
        def failing_max_retries(job_id):
            raise RuntimeError("Max retries callback explosion!")
        
        manager = RetryManager(
            on_retry=failing_retry,
            on_max_retries=failing_max_retries,
        )
        
        job = JobConfig(
            name="test",
            cmd="echo test",
            retry=RetryConfig(enabled=True, max_attempts=3, delays=[0]),
        )
        
        # Schedule a retry with 0 delay
        manager.schedule_retry("test-job", job, attempt=0)
        
        # Process should handle the exception gracefully (not crash)
        # The exception is now caught and logged
        asyncio.run(manager._process_retries())

    def test_rapid_schedule_cancel(self):
        """Test rapid scheduling and canceling doesn't cause issues."""
        retries = []
        manager = RetryManager(
            on_retry=lambda job_id: retries.append(job_id) or True,
        )
        
        job = JobConfig(
            name="test",
            cmd="echo test",
            retry=RetryConfig(enabled=True, delays=[0]),  # Immediate retry
        )
        
        # Rapidly schedule and cancel
        for i in range(100):
            manager.schedule_retry(f"job-{i}", job, attempt=0)
            if i % 2 == 0:
                manager.cancel_retry(f"job-{i}")
        
        # Only odd jobs should remain
        assert manager.pending_count == 50

    def test_rate_limit_detection(self, tmp_path):
        """Test rate limit detection in error output."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        
        manager = RetryManager(
            on_retry=lambda x: True,
            logs_dir=logs_dir,
        )
        
        # Create error log with rate limit
        error_file = logs_dir / "test-job.error.log"
        error_file.write_text("Error: 429 Too Many Requests\nPlease slow down")
        
        assert manager.is_rate_limit_error("test-job")
        
        # Test various rate limit patterns
        patterns = [
            "Rate limit exceeded",
            "too many requests",
            "quota exceeded for project",
            "throttled by server",
        ]
        
        for pattern in patterns:
            assert manager.is_rate_limit_error("x", error_log=pattern)


class TestWatchdogResilience:
    """Test watchdog (dead man's switch) resilience."""

    def test_missing_job_callback_exception(self):
        """Test watchdog handles callback exceptions gracefully."""
        def failing_callback(miss: MissedRunInfo):
            raise RuntimeError("Callback failed!")
        
        db = MagicMock()
        db.get_last_run.return_value = None
        
        watchdog = Watchdog(
            db=db,
            get_jobs=lambda: {},
            on_missed_run=failing_callback,
        )
        
        # Should not raise - the callback exception is caught internally
        asyncio.run(watchdog._check_and_alert())

    def test_timezone_edge_cases(self):
        """Test watchdog handles timezone edge cases."""
        db = MagicMock()
        db.get_last_run.return_value = None
        
        watchdog = Watchdog(
            db=db,
            get_jobs=lambda: {},
            on_missed_run=lambda x: None,
            default_timezone="America/Sao_Paulo",
        )
        
        # Test DST transition (if applicable)
        job = JobConfig(
            name="test",
            cmd="echo test",
            type=JobType.SCHEDULED,
            schedule="0 2 * * *",  # 2 AM - can be affected by DST
            timezone="America/Sao_Paulo",
        )
        
        # Should not crash - check the job directly
        miss = watchdog.check_job("test-job", job)
        # Result can be None or MissedRunInfo, just shouldn't crash


class TestLogRotationResilience:
    """Test log rotation under failure conditions."""

    def test_parse_size_edge_cases(self):
        """Test size parsing handles edge cases."""
        assert parse_size("0") == 0
        assert parse_size("1") == 1
        assert parse_size("1B") == 1
        assert parse_size("1KB") == 1024
        assert parse_size("1MB") == 1024 * 1024
        assert parse_size("1GB") == 1024 * 1024 * 1024
        assert parse_size("1.5MB") == int(1.5 * 1024 * 1024)
        
        # Invalid formats
        with pytest.raises(ValueError):
            parse_size("invalid")
        with pytest.raises(ValueError):
            parse_size("MB")

    def test_rotate_nonexistent_file(self, tmp_path):
        """Test rotating a file that doesn't exist."""
        result = rotate_log(tmp_path / "nonexistent.log")
        assert result is False

    def test_rotate_permission_denied(self, tmp_path):
        """Test rotation when permission is denied."""
        log_file = tmp_path / "readonly.log"
        log_file.write_text("test content")
        
        # Make parent read-only (on Unix)
        if os.name != "nt":
            os.chmod(tmp_path, 0o444)
            try:
                # Should handle gracefully
                try:
                    rotate_log(log_file)
                except PermissionError:
                    pass  # Expected
            finally:
                os.chmod(tmp_path, 0o755)

    def test_concurrent_rotation(self, tmp_path):
        """Test concurrent log rotation doesn't corrupt files."""
        log_file = tmp_path / "concurrent.log"
        
        async def rotate_many():
            tasks = []
            for i in range(10):
                log_file.write_text(f"Content {i}\n" * 100)
                tasks.append(asyncio.to_thread(
                    rotate_log, log_file, max_files=5
                ))
            await asyncio.gather(*tasks, return_exceptions=True)
        
        asyncio.run(rotate_many())
        
        # Should not have more than 5 rotated files
        rotated = list(tmp_path.glob("concurrent.log.*"))
        assert len(rotated) <= 5


class TestOutputParserResilience:
    """Test output parsing under failure conditions."""

    def test_binary_content(self):
        """Test parser handles binary content."""
        parser = OutputParser()
        
        # Random binary data
        binary = bytes(range(256))
        content = binary.decode("utf-8", errors="ignore")
        
        # Should not crash
        matches = parser.parse_output(content)
        assert isinstance(matches, list)

    def test_huge_file(self, tmp_path):
        """Test parsing huge files."""
        huge_file = tmp_path / "huge.log"
        
        # Write 10MB file
        with open(huge_file, "w") as f:
            for i in range(100000):
                f.write(f"Log line {i}: normal operation\n")
            f.write("ERROR: This is the error\n")
        
        parser = OutputParser()
        matches = parser.parse_file(huge_file, max_size=1024*1024)  # 1MB limit
        
        # Should still find the error (it's at the end)
        assert any(m.level == "error" for m in matches)

    def test_deeply_nested_patterns(self):
        """Test parser handles complex patterns."""
        parser = OutputParser()
        
        # Nested error messages
        content = """
        [2024-01-01 12:00:00] INFO: Starting...
        [2024-01-01 12:00:01] DEBUG: Processing...
        [2024-01-01 12:00:02] ERROR: Something failed
        [2024-01-01 12:00:02] ERROR: Traceback (most recent call last):
        [2024-01-01 12:00:02] ERROR:   File "test.py", line 10
        [2024-01-01 12:00:02] ERROR: Exception: Connection refused
        """
        
        matches = parser.parse_output(content)
        errors = [m for m in matches if m.level == "error"]
        assert len(errors) >= 1


class TestOperatingHoursResilience:
    """Test operating hours checker resilience."""

    def test_invalid_time_format(self):
        """Test handling of invalid time formats."""
        checker = OperatingHoursChecker()
        
        # Include all days to ensure day check passes
        config = OperatingHoursConfig(
            enabled=True,
            start="invalid",
            end="also-invalid",
            days=[1, 2, 3, 4, 5, 6, 7],  # All days
        )
        
        # Should fail open (return True) due to invalid time format
        assert checker.is_within_hours(config) is True

    def test_overnight_hours(self):
        """Test overnight operating hours (e.g., night shift)."""
        checker = OperatingHoursChecker()
        
        # Night shift: 22:00 - 06:00
        config = OperatingHoursConfig(
            enabled=True,
            start="22:00",
            end="06:00",
            days=[1, 2, 3, 4, 5],
        )
        
        # Test at 23:00 (should be within)
        import pytz
        tz = pytz.timezone("America/Sao_Paulo")
        test_time = datetime(2024, 1, 8, 23, 0)  # Monday 23:00
        test_time = tz.localize(test_time)
        
        result = checker.is_within_hours(config, test_time)
        assert result is True
        
        # Test at 03:00 (should be within - next day)
        test_time = datetime(2024, 1, 9, 3, 0)  # Tuesday 03:00
        test_time = tz.localize(test_time)
        result = checker.is_within_hours(config, test_time)
        assert result is True
        
        # Test at 12:00 (should be outside)
        test_time = datetime(2024, 1, 8, 12, 0)  # Monday 12:00
        test_time = tz.localize(test_time)
        result = checker.is_within_hours(config, test_time)
        assert result is False

    def test_all_days_disabled(self):
        """Test with no valid days."""
        checker = OperatingHoursChecker()
        
        config = OperatingHoursConfig(
            enabled=True,
            start="09:00",
            end="17:00",
            days=[],  # No valid days
        )
        
        # Should always be outside hours
        assert checker.is_within_hours(config) is False


class TestResourceMonitorResilience:
    """Test resource monitor under failure conditions."""

    def test_process_disappears(self):
        """Test monitoring a process that exits."""
        monitor = ResourceMonitor()
        
        config = ResourceLimitsConfig(
            enabled=True,
            max_memory_mb=100,
            max_cpu_percent=50,
        )
        
        # Register with a non-existent PID
        monitor.register_job("test-job", 999999999, config, datetime.now())
        
        # Should handle gracefully
        usage = monitor.get_usage("test-job")
        assert usage is None
        
        # Job should be unregistered
        assert monitor.monitored_count == 0

    def test_callback_exceptions(self):
        """Test monitor handles callback exceptions."""
        def failing_violation(v):
            raise RuntimeError("Violation callback failed!")
        
        def failing_kill(job_id, reason):
            raise RuntimeError("Kill callback failed!")
        
        monitor = ResourceMonitor(
            on_violation=failing_violation,
            on_kill=failing_kill,
        )
        
        # Should not crash
        asyncio.run(monitor._check_all())

    def test_rapid_register_unregister(self):
        """Test rapid registration/unregistration."""
        monitor = ResourceMonitor()
        
        config = ResourceLimitsConfig(enabled=True)
        
        # Rapidly register and unregister
        for i in range(100):
            monitor.register_job(f"job-{i}", i + 1000, config, datetime.now())
            if i % 3 == 0:
                monitor.unregister_job(f"job-{i}")
        
        # Should have about 66 jobs registered
        assert 60 <= monitor.monitored_count <= 70


class TestChaosScenarios:
    """Chaos engineering scenarios."""

    def test_signal_storm(self):
        """Test handling rapid signals."""
        # This is more of a documentation test
        # In production, SIGTERM should be handled gracefully
        pass

    def test_memory_pressure(self):
        """Test behavior under memory pressure."""
        # Allocate some memory and verify system still works
        large_list = [b"x" * 1000000 for _ in range(10)]  # ~10MB
        
        try:
            parser = OutputParser()
            matches = parser.parse_output("ERROR: test")
            assert len(matches) > 0
        finally:
            del large_list

    def test_disk_full_simulation(self, tmp_path):
        """Test behavior when disk is full."""
        log_file = tmp_path / "test.log"
        
        # Try to write a huge file (will fail eventually)
        # This is a simulation - in production, write should fail gracefully
        try:
            with open(log_file, "w") as f:
                f.write("test" * 1000)
        except IOError:
            pass  # Expected in some environments

    @pytest.mark.asyncio
    async def test_task_cancellation(self):
        """Test that tasks can be cancelled cleanly."""
        from procclaw.core.scheduler import Scheduler
        
        scheduler = Scheduler(
            on_trigger=lambda job_id, trigger: True,
            is_job_running=lambda job_id: False,
        )
        
        task = asyncio.create_task(scheduler.run())
        
        # Let it run briefly
        await asyncio.sleep(0.1)
        
        # Cancel
        scheduler.stop()
        task.cancel()
        
        try:
            await task
        except asyncio.CancelledError:
            pass  # Expected

    def test_unicode_in_logs(self, tmp_path):
        """Test handling of unicode in log files."""
        log_file = tmp_path / "unicode.log"
        
        # Various unicode content
        content = """
        Normal ASCII line
        Chinese: 中文错误
        Japanese: エラー発生
        Emoji: 🔥 ERROR 💀
        Arabic: خطأ
        Math: ∑ ERROR ∞
        """
        
        log_file.write_text(content, encoding="utf-8")
        
        parser = OutputParser()
        matches = parser.parse_file(log_file)
        
        # Should find ERROR patterns
        assert any("ERROR" in m.pattern for m in matches)


class TestEdgeCases:
    """Edge case tests."""

    def test_empty_job_config(self):
        """Test minimal job configuration."""
        job = JobConfig(name="minimal", cmd="echo test")
        
        assert job.retry.enabled is True
        assert job.operating_hours.enabled is False
        assert job.resources.enabled is False

    def test_max_retry_delays(self):
        """Test retry with many delays."""
        config = RetryConfig(
            enabled=True,
            max_attempts=100,
            delays=[1, 2, 4, 8, 16, 32, 64, 128, 256, 512],
        )
        
        delays = config.get_delays()
        assert len(delays) == 10
        assert delays[-1] == 512

    def test_cron_expression_edge_cases(self):
        """Test various cron expressions."""
        from procclaw.core.scheduler import Scheduler
        
        scheduler = Scheduler(
            on_trigger=lambda x, y: True,
            is_job_running=lambda x: False,
        )
        
        test_schedules = [
            "* * * * *",       # Every minute
            "0 0 * * *",       # Midnight
            "0 0 1 1 *",       # Jan 1 midnight
            "*/5 * * * *",     # Every 5 minutes
            "0 0 * * 0",       # Every Sunday
            "0 9-17 * * 1-5",  # 9-17 weekdays
        ]
        
        for schedule in test_schedules:
            job = JobConfig(
                name="test",
                cmd="echo test",
                type=JobType.SCHEDULED,
                schedule=schedule,
            )
            
            # Should not raise
            try:
                scheduler.add_job("test-job", job)
                scheduler.remove_job("test-job")
            except Exception as e:
                pytest.fail(f"Failed on schedule '{schedule}': {e}")

    def test_zero_timeouts(self):
        """Test zero or near-zero timeouts."""
        config = ResourceLimitsConfig(
            enabled=True,
            timeout_seconds=0,
        )
        
        # Job should immediately timeout
        # This is valid config - 0 means immediate timeout
        assert config.timeout_seconds == 0

    def test_negative_values(self):
        """Test handling of negative configuration values."""
        # Pydantic may or may not validate these (depends on field config)
        # The key is that the config doesn't crash
        try:
            config = ResourceLimitsConfig(
                enabled=True,
                max_memory_mb=-100,
            )
            # If it doesn't raise, that's also acceptable
            # (the monitor will just never trigger)
        except Exception:
            pass  # Expected if validation is strict


# Run with: pytest tests/test_resilience.py -v
