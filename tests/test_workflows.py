"""Tests for workflow features: ETA, Revocation, Results, Composition."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from procclaw.db import Database
from procclaw.core.eta import ETAScheduler, ETAJob
from procclaw.core.revocation import RevocationManager, Revocation
from procclaw.core.results import ResultCollector, JobResult, WorkflowResults
from procclaw.core.workflow import (
    WorkflowManager,
    WorkflowConfig,
    WorkflowRun,
    WorkflowType,
    WorkflowStatus,
    OnFailure,
)


class TestETAScheduler:
    """Tests for ETA scheduling."""

    def test_schedule_at_datetime(self):
        """Test scheduling at specific datetime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            triggered = []
            
            def on_trigger(job_id, trigger, params, key):
                triggered.append(job_id)
            
            scheduler = ETAScheduler(db=db, on_trigger=on_trigger)
            
            # Schedule for 1 second from now
            run_at = datetime.now(ZoneInfo("UTC")) + timedelta(seconds=1)
            job = scheduler.schedule_at("test-job", run_at)
            
            assert job.job_id == "test-job"
            assert not job.is_due
            
            # Wait for it to be due
            time.sleep(1.1)
            
            assert job.is_due
            triggered_jobs = scheduler.check_and_trigger()
            
            assert "test-job" in triggered_jobs
            assert "test-job" in triggered

    def test_schedule_in_seconds(self):
        """Test scheduling relative to now."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            scheduler = ETAScheduler(db=db)
            
            job = scheduler.schedule_in("test-job", seconds=2)
            
            assert job.job_id == "test-job"
            assert job.seconds_until > 1
            assert job.seconds_until < 3

    def test_cancel_eta(self):
        """Test cancelling ETA schedule."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            scheduler = ETAScheduler(db=db)
            
            run_at = datetime.now(ZoneInfo("UTC")) + timedelta(hours=1)
            scheduler.schedule_at("test-job", run_at)
            
            assert scheduler.get_eta("test-job") is not None
            
            cancelled = scheduler.cancel("test-job")
            assert cancelled
            
            assert scheduler.get_eta("test-job") is None

    def test_get_all_pending(self):
        """Test getting all pending ETA jobs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            scheduler = ETAScheduler(db=db)
            
            run_at = datetime.now(ZoneInfo("UTC")) + timedelta(hours=1)
            scheduler.schedule_at("job1", run_at)
            scheduler.schedule_at("job2", run_at)
            scheduler.schedule_at("job3", run_at)
            
            pending = scheduler.get_all_pending()
            assert len(pending) == 3

    def test_timezone_handling(self):
        """Test timezone conversion."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            scheduler = ETAScheduler(db=db, default_timezone="America/Sao_Paulo")
            
            # Schedule in local time
            local_time = datetime(2025, 1, 15, 10, 0, 0)  # No tzinfo
            job = scheduler.schedule_at("test-job", local_time)
            
            # Should be stored in UTC
            assert job.run_at.tzinfo == ZoneInfo("UTC")

    def test_persistence(self):
        """Test ETA jobs persist across restarts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            
            # First scheduler
            db1 = Database(db_path)
            scheduler1 = ETAScheduler(db=db1)
            run_at = datetime.now(ZoneInfo("UTC")) + timedelta(hours=1)
            scheduler1.schedule_at("persistent-job", run_at)
            
            # Second scheduler (simulate restart)
            db2 = Database(db_path)
            scheduler2 = ETAScheduler(db=db2)
            
            job = scheduler2.get_eta("persistent-job")
            assert job is not None
            assert job.job_id == "persistent-job"


class TestRevocationManager:
    """Tests for task revocation."""

    def test_revoke_job(self):
        """Test revoking a job."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            manager = RevocationManager(db=db)
            
            revocation = manager.revoke("test-job", reason="Testing")
            
            assert revocation.job_id == "test-job"
            assert revocation.reason == "Testing"
            assert manager.is_revoked("test-job")

    def test_revoke_with_terminate(self):
        """Test revoke with terminate flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            manager = RevocationManager(db=db)
            
            manager.revoke("test-job", terminate=True)
            
            assert manager.should_terminate("test-job")

    def test_unrevoke(self):
        """Test unrevoking a job."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            manager = RevocationManager(db=db)
            
            manager.revoke("test-job")
            assert manager.is_revoked("test-job")
            
            manager.unrevoke("test-job")
            assert not manager.is_revoked("test-job")

    def test_revocation_expiry(self):
        """Test revocation expires after timeout."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            manager = RevocationManager(db=db)
            
            manager.revoke("test-job", expires_in=1)
            assert manager.is_revoked("test-job")
            
            time.sleep(1.1)
            
            assert not manager.is_revoked("test-job")

    def test_consume_revocation(self):
        """Test consuming (one-time use) revocation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            manager = RevocationManager(db=db)
            
            manager.revoke("test-job")
            
            revocation = manager.consume_revocation("test-job")
            assert revocation is not None
            
            # Should be gone after consume
            assert manager.get_revocation("test-job") is None

    def test_cleanup_expired(self):
        """Test cleanup of expired revocations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            manager = RevocationManager(db=db)
            
            manager.revoke("job1", expires_in=1)
            manager.revoke("job2", expires_in=3600)
            
            time.sleep(1.1)
            
            cleaned = manager.cleanup_expired()
            assert cleaned == 1
            
            assert not manager.is_revoked("job1")
            assert manager.is_revoked("job2")


class TestResultCollector:
    """Tests for result aggregation."""

    def test_capture_result(self):
        """Test capturing job result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            
            # Create a log file
            (logs_dir / "test-job.log").write_text("Output line 1\nOutput line 2\n")
            
            collector = ResultCollector(db=db, logs_dir=logs_dir)
            
            result = collector.capture(
                job_id="test-job",
                run_id=1,
                exit_code=0,
                duration_seconds=5.5,
            )
            
            assert result.job_id == "test-job"
            assert result.exit_code == 0
            assert result.success
            assert "Output line" in result.stdout_tail

    def test_result_to_env_vars(self):
        """Test converting result to env vars."""
        result = JobResult(
            job_id="test-job",
            run_id=1,
            exit_code=0,
            stdout_tail="Hello World",
            duration_seconds=3.5,
        )
        
        env = result.to_env_vars()
        
        assert env["PROCCLAW_PREV_JOB_ID"] == "test-job"
        assert env["PROCCLAW_PREV_EXIT_CODE"] == "0"
        assert env["PROCCLAW_PREV_SUCCESS"] == "1"
        assert "Hello World" in env["PROCCLAW_PREV_STDOUT"]

    def test_workflow_results(self):
        """Test workflow result aggregation."""
        results = WorkflowResults(
            workflow_run_id=1,
            workflow_id="test-workflow",
        )
        
        results.add_result(JobResult(job_id="job1", run_id=1, exit_code=0, duration_seconds=1))
        results.add_result(JobResult(job_id="job2", run_id=2, exit_code=0, duration_seconds=2))
        results.add_result(JobResult(job_id="job3", run_id=3, exit_code=1, duration_seconds=3))
        
        assert results.count_success == 2
        assert results.count_failed == 1
        assert not results.all_success
        assert results.any_failed

    def test_workflow_results_to_env(self):
        """Test workflow results to env vars."""
        results = WorkflowResults(
            workflow_run_id=1,
            workflow_id="test-workflow",
        )
        
        results.add_result(JobResult(job_id="job1", run_id=1, exit_code=0, duration_seconds=1))
        results.add_result(JobResult(job_id="job2", run_id=2, exit_code=0, duration_seconds=2))
        
        env = results.to_env_vars()
        
        assert env["PROCCLAW_WORKFLOW_ID"] == "test-workflow"
        assert env["PROCCLAW_WORKFLOW_SUCCESS"] == "1"
        assert "job1" in env["PROCCLAW_WORKFLOW_JOB_IDS"]
        assert "job2" in env["PROCCLAW_WORKFLOW_JOB_IDS"]

    def test_write_results_file(self):
        """Test writing results to JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            results = WorkflowResults(
                workflow_run_id=1,
                workflow_id="test-workflow",
            )
            
            results.add_result(JobResult(job_id="job1", run_id=1, exit_code=0, duration_seconds=1))
            
            path = results.write_results_file(Path(tmpdir))
            
            assert Path(path).exists()
            
            with open(path) as f:
                data = json.load(f)
            
            assert data["workflow_id"] == "test-workflow"
            assert "job1" in data["results"]


class TestWorkflowConfig:
    """Tests for workflow configuration."""

    def test_chain_config(self):
        """Test chain workflow configuration."""
        config = WorkflowConfig(
            id="deploy-pipeline",
            name="Deploy Pipeline",
            type=WorkflowType.CHAIN,
            jobs=["build", "test", "deploy"],
        )
        
        assert config.type == WorkflowType.CHAIN
        assert len(config.jobs) == 3

    def test_group_config(self):
        """Test group workflow configuration."""
        config = WorkflowConfig(
            id="parallel-tests",
            name="Parallel Tests",
            type=WorkflowType.GROUP,
            jobs=["test-unit", "test-integration", "test-e2e"],
            max_parallel=5,
        )
        
        assert config.type == WorkflowType.GROUP
        assert config.max_parallel == 5

    def test_chord_config(self):
        """Test chord workflow configuration."""
        config = WorkflowConfig(
            id="process-and-notify",
            name="Process and Notify",
            type=WorkflowType.CHORD,
            jobs=["process-a", "process-b", "process-c"],
            callback="send-notification",
        )
        
        assert config.type == WorkflowType.CHORD
        assert config.callback == "send-notification"

    def test_config_serialization(self):
        """Test config serialization/deserialization."""
        config = WorkflowConfig(
            id="test",
            name="Test Workflow",
            type=WorkflowType.CHAIN,
            jobs=["job1", "job2"],
            on_failure=OnFailure.CONTINUE,
        )
        
        data = config.to_dict()
        restored = WorkflowConfig.from_dict(data)
        
        assert restored.id == config.id
        assert restored.type == config.type
        assert restored.on_failure == config.on_failure


class TestWorkflowRun:
    """Tests for workflow run state."""

    def test_run_status(self):
        """Test workflow run status transitions."""
        config = WorkflowConfig(
            id="test",
            name="Test",
            type=WorkflowType.CHAIN,
            jobs=["job1"],
        )
        
        run = WorkflowRun(id=1, workflow_id="test", config=config)
        
        assert run.status == WorkflowStatus.PENDING
        assert not run.is_running
        assert not run.is_finished
        
        run.status = WorkflowStatus.RUNNING
        assert run.is_running
        
        run.status = WorkflowStatus.COMPLETED
        assert run.is_finished
        assert not run.is_running


class TestWorkflowIntegration:
    """Integration tests for workflows."""

    @pytest.fixture
    def workflow_env(self):
        """Create workflow test environment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            
            result_collector = ResultCollector(db=db, logs_dir=logs_dir)
            
            # Track started jobs
            started_jobs = []
            completed_jobs = {}
            
            def start_job(job_id, trigger, env):
                started_jobs.append(job_id)
                return True
            
            def is_running(job_id):
                return job_id in started_jobs and job_id not in completed_jobs
            
            def stop_job(job_id):
                if job_id in started_jobs:
                    completed_jobs[job_id] = 1
                return True
            
            manager = WorkflowManager(
                db=db,
                result_collector=result_collector,
                start_job=start_job,
                is_job_running=is_running,
                stop_job=stop_job,
                logs_dir=logs_dir,
            )
            
            yield {
                "manager": manager,
                "db": db,
                "logs_dir": logs_dir,
                "result_collector": result_collector,
                "started_jobs": started_jobs,
                "completed_jobs": completed_jobs,
            }

    def test_register_workflow(self, workflow_env):
        """Test registering a workflow."""
        manager = workflow_env["manager"]
        
        config = WorkflowConfig(
            id="test-workflow",
            name="Test Workflow",
            type=WorkflowType.CHAIN,
            jobs=["job1", "job2"],
        )
        
        manager.register_workflow(config)
        
        retrieved = manager.get_workflow("test-workflow")
        assert retrieved is not None
        assert retrieved.name == "Test Workflow"

    def test_list_workflows(self, workflow_env):
        """Test listing workflows."""
        manager = workflow_env["manager"]
        
        config1 = WorkflowConfig(id="w1", name="W1", type=WorkflowType.CHAIN, jobs=["j1"])
        config2 = WorkflowConfig(id="w2", name="W2", type=WorkflowType.GROUP, jobs=["j2"])
        
        manager.register_workflow(config1)
        manager.register_workflow(config2)
        
        workflows = manager.list_workflows()
        assert len(workflows) == 2

    def test_start_workflow_not_found(self, workflow_env):
        """Test starting non-existent workflow."""
        manager = workflow_env["manager"]
        
        run = manager.start_workflow("non-existent")
        assert run is None

    def test_workflow_run_tracking(self, workflow_env):
        """Test workflow run is tracked."""
        manager = workflow_env["manager"]
        
        config = WorkflowConfig(
            id="tracked-workflow",
            name="Tracked",
            type=WorkflowType.CHAIN,
            jobs=["job1"],
        )
        manager.register_workflow(config)
        
        run = manager.start_workflow("tracked-workflow")
        
        assert run is not None
        assert run.workflow_id == "tracked-workflow"
        assert run.status == WorkflowStatus.RUNNING

    def test_cancel_workflow(self, workflow_env):
        """Test cancelling a workflow."""
        manager = workflow_env["manager"]
        
        config = WorkflowConfig(
            id="cancellable",
            name="Cancellable",
            type=WorkflowType.CHAIN,
            jobs=["job1", "job2", "job3"],
        )
        manager.register_workflow(config)
        
        run = manager.start_workflow("cancellable")
        assert run is not None
        
        cancelled = manager.cancel_workflow(run.id)
        assert cancelled
        
        run = manager.get_run(run.id)
        assert run.status == WorkflowStatus.CANCELLED


class TestMissionCritical:
    """Mission-critical tests for edge cases and failure scenarios."""

    def test_eta_past_time_triggers_immediately(self):
        """ETA in the past should trigger immediately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            triggered = []
            
            def on_trigger(job_id, trigger, params, key):
                triggered.append(job_id)
            
            scheduler = ETAScheduler(db=db, on_trigger=on_trigger)
            
            # Schedule for 1 hour ago
            past_time = datetime.now(ZoneInfo("UTC")) - timedelta(hours=1)
            job = scheduler.schedule_at("past-job", past_time)
            
            assert job.is_due
            scheduler.check_and_trigger()
            
            assert "past-job" in triggered

    def test_revocation_race_condition(self):
        """Test revocation under concurrent access."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            manager = RevocationManager(db=db)
            
            # Revoke and check in parallel
            import threading
            
            results = []
            
            def revoke_and_check():
                manager.revoke("race-job", expires_in=1)
                is_revoked = manager.is_revoked("race-job")
                results.append(is_revoked)
            
            threads = [threading.Thread(target=revoke_and_check) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            
            # At least one should see it as revoked
            assert any(results)

    def test_result_capture_with_empty_logs(self):
        """Test result capture when log files don't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            
            collector = ResultCollector(db=db, logs_dir=logs_dir)
            
            # Capture without any log files
            result = collector.capture(
                job_id="no-logs-job",
                run_id=1,
                exit_code=0,
                duration_seconds=1.0,
            )
            
            assert result.stdout_tail == ""
            assert result.stderr_tail == ""

    def test_workflow_on_failure_stop(self):
        """Test workflow stops on failure when configured."""
        config = WorkflowConfig(
            id="stop-on-fail",
            name="Stop on Fail",
            type=WorkflowType.CHAIN,
            jobs=["job1", "job2", "job3"],
            on_failure=OnFailure.STOP,
        )
        
        assert config.on_failure == OnFailure.STOP

    def test_workflow_on_failure_continue(self):
        """Test workflow continues on failure when configured."""
        config = WorkflowConfig(
            id="continue-on-fail",
            name="Continue on Fail",
            type=WorkflowType.CHAIN,
            jobs=["job1", "job2", "job3"],
            on_failure=OnFailure.CONTINUE,
        )
        
        assert config.on_failure == OnFailure.CONTINUE

    def test_large_stdout_truncation(self):
        """Test large stdout is truncated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            
            # Create a large log file (1MB)
            large_content = "x" * (1024 * 1024)
            (logs_dir / "large-job.log").write_text(large_content)
            
            collector = ResultCollector(
                db=db,
                logs_dir=logs_dir,
                stdout_tail_chars=1000,
            )
            
            result = collector.capture(
                job_id="large-job",
                run_id=1,
                exit_code=0,
                duration_seconds=1.0,
            )
            
            # Should be truncated
            assert len(result.stdout_tail) <= 1000

    def test_eta_idempotency_key(self):
        """Test ETA with idempotency key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            triggered = []
            
            def on_trigger(job_id, trigger, params, key):
                triggered.append((job_id, key))
            
            scheduler = ETAScheduler(db=db, on_trigger=on_trigger)
            
            run_at = datetime.now(ZoneInfo("UTC")) + timedelta(seconds=0.1)
            scheduler.schedule_at(
                "idempotent-job",
                run_at,
                idempotency_key="unique-123",
            )
            
            time.sleep(0.2)
            scheduler.check_and_trigger()
            
            assert len(triggered) == 1
            assert triggered[0][1] == "unique-123"

    def test_workflow_result_passing(self):
        """Test results are passed between jobs in chain."""
        result = JobResult(
            job_id="first-job",
            run_id=1,
            exit_code=0,
            stdout_tail="Result data",
            duration_seconds=1.0,
        )
        
        env = result.to_env_vars()
        
        assert "PROCCLAW_PREV_JOB_ID" in env
        assert env["PROCCLAW_PREV_JOB_ID"] == "first-job"
        assert "Result data" in env["PROCCLAW_PREV_STDOUT"]
