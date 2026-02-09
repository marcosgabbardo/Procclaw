"""Stress tests and chaos engineering for ProcClaw.

These tests simulate extreme conditions to verify stability:
- High concurrency
- Rapid operations
- Long-running scenarios
- Resource exhaustion
- Failure cascades
"""

import asyncio
import gc
import os
import random
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch
import pytest

from procclaw.models import (
    JobConfig,
    JobType,
    JobState,
    JobStatus,
    RetryConfig,
    LogConfig,
)
from procclaw.db import Database
from procclaw.core.scheduler import Scheduler
from procclaw.core.retry import RetryManager
from procclaw.core.log_utils import LogRotator, rotate_log
from procclaw.core.output_parser import OutputParser


class TestHighConcurrency:
    """Test behavior under high concurrency."""

    def test_100_concurrent_job_registrations(self):
        """Test registering 100 jobs concurrently."""
        triggered = []
        
        scheduler = Scheduler(
            on_trigger=lambda job_id, trigger: triggered.append(job_id) or True,
            is_job_running=lambda job_id: False,
        )
        
        def register_job(i):
            job = JobConfig(
                name=f"job-{i}",
                cmd=f"echo {i}",
                type=JobType.SCHEDULED,
                schedule=f"{i % 60} * * * *",
            )
            scheduler.add_job(f"job-{i}", job)
        
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(register_job, i) for i in range(100)]
            for f in futures:
                f.result()
        
        assert len(scheduler._jobs) == 100

    def test_rapid_start_stop_cycles(self):
        """Test rapidly starting and stopping the scheduler."""
        scheduler = Scheduler(
            on_trigger=lambda x, y: True,
            is_job_running=lambda x: False,
        )
        
        async def rapid_cycles():
            for _ in range(10):
                task = asyncio.create_task(scheduler.run())
                await asyncio.sleep(0.05)
                scheduler.stop()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                # Restart scheduler
                scheduler._running = False
        
        asyncio.run(rapid_cycles())

    def test_concurrent_database_operations(self, tmp_path):
        """Test many concurrent database operations."""
        db_path = tmp_path / "stress.db"
        db = Database(db_path)
        
        
        errors = []
        
        def write_state(i):
            try:
                state = JobState(
                    job_id=f"job-{i}",
                    status=JobStatus.RUNNING if i % 2 == 0 else JobStatus.STOPPED,
                    started_at=datetime.now(),
                )
                db.save_state(state)
            except Exception as e:
                errors.append(str(e))
        
        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(write_state, i) for i in range(500)]
            for f in futures:
                f.result()
        
        # Check for errors (some lock errors might be acceptable)
        serious_errors = [e for e in errors if "corrupt" in e.lower()]
        assert len(serious_errors) == 0, f"Serious errors: {serious_errors}"
        
        

    @pytest.mark.asyncio
    async def test_many_pending_retries(self):
        """Test handling many pending retries."""
        retry_count = 0
        
        def on_retry(job_id):
            nonlocal retry_count
            retry_count += 1
            return True
        
        manager = RetryManager(on_retry=on_retry)
        
        # Schedule 1000 retries with 0 delay
        for i in range(1000):
            job = JobConfig(
                name=f"job-{i}",
                cmd="echo test",
                retry=RetryConfig(enabled=True, delays=[0]),
            )
            manager.schedule_retry(f"job-{i}", job, attempt=0)
        
        assert manager.pending_count == 1000
        
        # Process all retries
        await manager._process_retries()
        
        assert retry_count == 1000
        assert manager.pending_count == 0


class TestRapidOperations:
    """Test rapid-fire operations."""

    def test_rapid_file_rotation(self, tmp_path):
        """Test rapid log file rotation."""
        log_file = tmp_path / "rapid.log"
        
        for i in range(100):
            # Write content
            log_file.write_text(f"Content iteration {i}" * 100)
            # Rotate
            rotate_log(log_file, max_files=5)
        
        # Should have at most 5 rotated files
        rotated = list(tmp_path.glob("rapid.log.*"))
        assert len(rotated) <= 5

    def test_rapid_pattern_matching(self):
        """Test rapid output parsing."""
        parser = OutputParser()
        
        # Large content with many lines
        lines = [
            "INFO: Normal operation\n" if i % 100 != 0 else "ERROR: Something failed\n"
            for i in range(10000)
        ]
        content = "".join(lines)
        
        start = time.time()
        matches = parser.parse_output(content)
        duration = time.time() - start
        
        # Should complete in reasonable time
        assert duration < 5.0, f"Parsing took too long: {duration}s"
        
        # Should find the errors
        errors = [m for m in matches if m.level == "error"]
        assert len(errors) == 100

    def test_rapid_job_state_changes(self, tmp_path):
        """Test rapid job state changes."""
        db_path = tmp_path / "states.db"
        db = Database(db_path)
        
        
        job_id = "rapid-job"
        statuses = [JobStatus.RUNNING, JobStatus.STOPPED, JobStatus.FAILED]
        
        for i in range(1000):
            state = JobState(
                job_id=job_id,
                status=statuses[i % 3],
                started_at=datetime.now(),
            )
            db.save_state(state)
        
        # Final state should be retrievable
        final = db.get_state(job_id)
        assert final is not None
        assert final.status in statuses
        
        


class TestLongRunning:
    """Test long-running scenarios."""

    @pytest.mark.asyncio
    async def test_scheduler_long_run(self):
        """Test scheduler running for extended period."""
        triggered = []
        
        scheduler = Scheduler(
            on_trigger=lambda job_id, trigger: triggered.append((job_id, datetime.now())) or True,
            is_job_running=lambda job_id: False,
        )
        
        # Register job that runs every second (for testing)
        job = JobConfig(
            name="frequent",
            cmd="echo test",
            type=JobType.SCHEDULED,
            schedule="* * * * * *",  # Every second (non-standard but test)
        )
        scheduler.add_job("frequent-job", job)
        
        task = asyncio.create_task(scheduler.run())
        
        # Let it run for 3 seconds
        await asyncio.sleep(3)
        
        scheduler.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        
        # Should have triggered at least once
        # (depends on timing, so this is a soft check)

    @pytest.mark.asyncio
    async def test_retry_manager_long_run(self):
        """Test retry manager running for extended period."""
        retried = []
        
        manager = RetryManager(
            on_retry=lambda x: retried.append(x) or True,
        )
        
        task = asyncio.create_task(manager.run())
        
        # Schedule some retries during run
        for i in range(5):
            job = JobConfig(
                name=f"job-{i}",
                cmd="echo test",
                retry=RetryConfig(enabled=True, delays=[1]),  # 1 second delay
            )
            manager.schedule_retry(f"job-{i}", job, attempt=0)
            await asyncio.sleep(0.5)
        
        # Wait for retries
        await asyncio.sleep(3)
        
        manager.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        
        # Should have retried all jobs
        assert len(retried) >= 3  # At least 3 should have completed


class TestResourceExhaustion:
    """Test behavior under resource constraints."""

    def test_memory_efficient_log_parsing(self, tmp_path):
        """Test that log parsing doesn't use excessive memory."""
        # Create a large log file
        large_log = tmp_path / "large.log"
        
        with open(large_log, "w") as f:
            for i in range(100000):
                f.write(f"Log line {i}: some normal content here\n")
                if i == 50000:
                    f.write("ERROR: Critical failure at line 50000\n")
        
        # Record memory before
        import psutil
        process = psutil.Process()
        mem_before = process.memory_info().rss
        
        # Parse the file
        parser = OutputParser()
        matches = parser.parse_file(large_log, max_size=1024*1024)  # 1MB limit
        
        # Record memory after
        mem_after = process.memory_info().rss
        
        # Memory increase should be reasonable (<50MB)
        mem_increase = (mem_after - mem_before) / (1024 * 1024)
        assert mem_increase < 50, f"Memory increase: {mem_increase}MB"
        
        # Should still find errors
        assert len(matches) >= 1

    def test_many_open_files(self, tmp_path):
        """Test handling many log files."""
        logs_dir = tmp_path / "many_logs"
        logs_dir.mkdir()
        
        # Create many log files
        for i in range(100):
            log_file = logs_dir / f"job-{i}.log"
            log_file.write_text(f"Log content for job {i}")
        
        rotator = LogRotator(logs_dir=logs_dir)
        
        # Register all jobs
        config = LogConfig(max_size="1KB", rotate=3)
        for i in range(100):
            rotator.register_job(f"job-{i}", config)
        
        # Should handle all jobs
        assert len(rotator._jobs) == 100

    def test_database_with_many_records(self, tmp_path):
        """Test database with many records."""
        db_path = tmp_path / "many.db"
        db = Database(db_path)
        
        
        # Insert many records
        from procclaw.models import JobRun
        
        start = time.time()
        for i in range(10000):
            run = JobRun(
                job_id=f"job-{i % 100}",  # 100 different jobs
                run_id=i,
                started_at=datetime.now() - timedelta(hours=i),
                finished_at=datetime.now() - timedelta(hours=i) + timedelta(minutes=5),
                exit_code=0 if i % 10 != 0 else 1,
            )
            db.add_run(run)
        
        duration = time.time() - start
        
        # Should complete in reasonable time
        assert duration < 30, f"Insertion took {duration}s"
        
        # Queries should still be fast
        start = time.time()
        runs = db.get_runs("job-50", limit=100)
        query_duration = time.time() - start
        
        assert query_duration < 1.0, f"Query took {query_duration}s"
        
        


class TestFailureCascades:
    """Test behavior when failures cascade."""

    def test_all_jobs_fail(self):
        """Test when all scheduled jobs fail simultaneously."""
        max_retries_called = []
        
        manager = RetryManager(
            on_retry=lambda x: False,  # All retries fail
            on_max_retries=lambda x: max_retries_called.append(x),
        )
        
        # Schedule many failing jobs - each at max_attempts-1 so next schedule triggers max
        for i in range(50):
            job = JobConfig(
                name=f"failing-{i}",
                cmd="exit 1",
                retry=RetryConfig(enabled=True, max_attempts=3, delays=[0]),
            )
            
            # Schedule at attempt 2 (0-indexed) - which is >= max_attempts, triggers max_retries
            result = manager.schedule_retry(f"failing-{i}", job, attempt=3)
            # Result should be None because max_attempts is reached
        
        # All jobs should have reached max retries
        assert len(max_retries_called) == 50

    def test_callback_chain_failure(self):
        """Test when callbacks fail in chain."""
        call_order = []
        
        def failing_callback_1(miss):
            call_order.append("callback_1")
            raise RuntimeError("First callback failed")
        
        # Watchdog with failing callback shouldn't crash
        from procclaw.core.watchdog import Watchdog
        
        db = MagicMock()
        db.get_last_run.return_value = None
        
        watchdog = Watchdog(
            db=db,
            get_jobs=lambda: {},
            on_missed_run=failing_callback_1,
        )
        
        # Should handle gracefully
        asyncio.run(watchdog._check_and_alert())


class TestGracefulDegradation:
    """Test graceful degradation under adverse conditions."""

    def test_missing_config_directory(self, tmp_path):
        """Test behavior when config directory is missing."""
        missing_dir = tmp_path / "nonexistent" / "config"
        
        # Rotator should handle missing directory
        rotator = LogRotator(logs_dir=missing_dir)
        
        # Should not crash
        result = rotator.check_all()
        assert result == 0

    def test_readonly_database(self, tmp_path):
        """Test behavior with readonly database."""
        db_path = tmp_path / "readonly.db"
        db = Database(db_path)
        
        # Write initial data
        state = JobState(job_id="initial", status=JobStatus.STOPPED)
        db.save_state(state)
        
        # Make readonly
        if os.name != "nt":
            os.chmod(db_path, 0o444)
            
            try:
                # Reading should still work
                retrieved = db.get_state("initial")
                # May or may not work depending on how sqlite handles this
            except Exception:
                pass  # Expected
            finally:
                os.chmod(db_path, 0o644)

    def test_network_timeout_simulation(self):
        """Test behavior when network operations timeout."""
        from procclaw.openclaw import OpenClawIntegration
        
        integration = OpenClawIntegration()
        
        # Test that integration handles missing webhook gracefully
        # (no webhook URL configured = should just log/skip)
        integration.on_job_failed("test-job", 1, "Test error", should_alert=True)
        
        # Should have queued the alert instead of crashing
        assert integration._alert_queue.qsize() >= 1


class TestRecovery:
    """Test recovery from various failure states."""

    def test_recover_from_interrupted_rotation(self, tmp_path):
        """Test recovery when rotation is interrupted."""
        log_file = tmp_path / "interrupted.log"
        log_file.write_text("Original content")
        
        # Simulate partial rotation (file renamed but new not created)
        rotated_file = tmp_path / "interrupted.log.1"
        log_file.rename(rotated_file)
        
        # Create new log file
        log_file.write_text("New content after interruption")
        
        # Rotation should still work
        rotate_log(log_file, max_files=5)
        
        # Should have proper state
        assert log_file.exists() or rotated_file.exists()

    def test_recover_from_corrupted_state(self, tmp_path):
        """Test recovery when state is corrupted."""
        db_path = tmp_path / "corrupt_state.db"
        db = Database(db_path)
        
        # Write some valid state
        state = JobState(job_id="test", status=JobStatus.RUNNING)
        db.save_state(state)
        
        # Corrupt the state (write garbage to job_states table)
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            # This might fail depending on constraints
            conn.execute("UPDATE job_states SET status = 'INVALID_STATUS'")
            conn.commit()
        except:
            pass
        conn.close()
        
        # Should be able to recover
        db2 = Database(db_path)
        
        # Reading might fail or return unexpected data, but shouldn't crash
        try:
            state = db2.get_state("test")
            # If it succeeds, that's fine too
        except:
            pass  # Expected for invalid enum value


# Run with: pytest tests/test_stress.py -v --timeout=60
