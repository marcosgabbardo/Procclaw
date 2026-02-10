"""Integration tests for job wrapper with the actual supervisor.

These tests verify that:
1. Jobs use the wrapper automatically
2. Completion markers are created and cleaned up
3. Heartbeats work during job execution
4. Recovery from daemon crash works correctly
"""

import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

from procclaw.core.job_wrapper import (
    JobWrapperManager,
    get_wrapper_manager,
    DEFAULT_MARKER_DIR,
    DEFAULT_HEARTBEAT_DIR,
)
from procclaw.core.supervisor import Supervisor
from procclaw.config import load_config, load_jobs
from procclaw.db import Database
from procclaw.models import JobConfig, JobStatus, JobType


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing."""
    workspace = Path(tempfile.mkdtemp(prefix="procclaw_test_"))
    
    # Create directories
    (workspace / "logs").mkdir()
    (workspace / "state").mkdir()
    
    yield workspace
    
    # Cleanup
    shutil.rmtree(workspace, ignore_errors=True)


@pytest.fixture
def test_job_config():
    """Create a simple test job config."""
    return {
        "test-wrapper-job": {
            "name": "Test Wrapper Job",
            "cmd": "echo 'Hello from wrapper' && sleep 1 && exit 0",
            "type": "manual",
        }
    }


@pytest.fixture
def test_failing_job_config():
    """Create a job that fails."""
    return {
        "test-fail-job": {
            "name": "Test Failing Job",
            "cmd": "echo 'About to fail' && exit 42",
            "type": "manual",
        }
    }


class TestWrapperBasicIntegration:
    """Basic integration tests for wrapper functionality."""
    
    def test_wrapper_manager_singleton(self):
        """Test that wrapper manager is a singleton."""
        m1 = get_wrapper_manager()
        m2 = get_wrapper_manager()
        assert m1 is m2
    
    def test_wrapper_directories_created(self):
        """Test that wrapper directories are created on startup."""
        m = get_wrapper_manager()
        assert m.marker_dir.exists()
        assert m.heartbeat_dir.exists()
    
    def test_wrapper_script_exists(self):
        """Test that wrapper script is created."""
        m = get_wrapper_manager()
        assert m.wrapper_path.exists()
        assert os.access(m.wrapper_path, os.X_OK)


class TestJobExecutionWithWrapper:
    """Test job execution through wrapper."""
    
    def test_simple_job_creates_marker(self):
        """Test that running a job through wrapper creates marker."""
        m = get_wrapper_manager()
        
        # Clean up first
        for f in m.marker_dir.glob("*.done"):
            f.unlink()
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "integration-test-1"
        env["PROCCLAW_RUN_ID"] = "9001"
        
        wrapped = m.wrap_command("echo 'test' && exit 0")
        result = subprocess.run(wrapped, shell=True, env=env, capture_output=True)
        
        time.sleep(0.5)
        
        marker = m.check_completion_marker("integration-test-1", 9001)
        assert marker is not None
        assert marker.exit_code == 0
        
        # Cleanup
        m.cleanup_marker("integration-test-1", 9001)
    
    def test_failing_job_records_exit_code(self):
        """Test that failing job records correct exit code."""
        m = get_wrapper_manager()
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "integration-test-2"
        env["PROCCLAW_RUN_ID"] = "9002"
        
        wrapped = m.wrap_command("exit 123")
        result = subprocess.run(wrapped, shell=True, env=env, capture_output=True)
        
        time.sleep(0.5)
        
        marker = m.check_completion_marker("integration-test-2", 9002)
        assert marker is not None
        assert marker.exit_code == 123
        
        # Cleanup
        m.cleanup_marker("integration-test-2", 9002)
    
    def test_long_running_job_has_heartbeat(self):
        """Test that long-running job produces heartbeats."""
        m = get_wrapper_manager()
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "integration-test-3"
        env["PROCCLAW_RUN_ID"] = "9003"
        
        wrapped = m.wrap_command("sleep 10")
        proc = subprocess.Popen(
            wrapped,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        
        try:
            # Wait for heartbeat to appear
            time.sleep(2)
            
            status = m.check_heartbeat("integration-test-3", 9003)
            assert status is not None
            assert status.is_alive
        finally:
            os.killpg(os.getpgid(proc.pid), 15)
            proc.wait(timeout=5)
            m.cleanup_marker("integration-test-3", 9003)
            m.cleanup_heartbeat("integration-test-3", 9003)


class TestCrashRecovery:
    """Test recovery from simulated daemon crashes."""
    
    def test_marker_persists_after_job_completes(self):
        """Test that marker persists even if not immediately read."""
        m = get_wrapper_manager()
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "crash-test-1"
        env["PROCCLAW_RUN_ID"] = "9010"
        
        # Run job
        wrapped = m.wrap_command("echo 'done' && exit 7")
        subprocess.run(wrapped, shell=True, env=env, capture_output=True)
        
        time.sleep(0.5)
        
        # Simulate daemon being down for a while
        time.sleep(2)
        
        # "Daemon restarts" and checks marker
        marker = m.check_completion_marker("crash-test-1", 9010)
        assert marker is not None
        assert marker.exit_code == 7
        
        # Cleanup
        m.cleanup_marker("crash-test-1", 9010)
    
    def test_stale_heartbeat_detected(self):
        """Test that stale heartbeat is detected as dead job."""
        m = get_wrapper_manager()
        
        # Create a fake stale heartbeat (simulating job that died mid-execution)
        hb_path = m.get_heartbeat_path("crash-test-2", 9011)
        old_time = int(time.time()) - 60  # 1 minute ago
        hb_path.write_text(str(old_time))
        
        status = m.check_heartbeat("crash-test-2", 9011)
        assert status is not None
        assert not status.is_alive
        assert status.age_seconds > 50
        
        # Cleanup
        m.cleanup_heartbeat("crash-test-2", 9011)
    
    def test_no_marker_no_heartbeat_is_unknown(self):
        """Test behavior when there's no marker and no heartbeat."""
        m = get_wrapper_manager()
        
        # Clean state
        marker = m.check_completion_marker("crash-test-3", 9012)
        heartbeat = m.check_heartbeat("crash-test-3", 9012)
        
        assert marker is None
        assert heartbeat is None
        # In this case, the job status is unknown - could have crashed at any point


class TestWrapperCleanup:
    """Test cleanup functionality."""
    
    def test_cleanup_stale_markers(self):
        """Test that old markers are cleaned up."""
        m = get_wrapper_manager()
        
        # Create an old marker
        old_marker = m.get_marker_path("old-job", 8000)
        old_marker.write_text("0")
        old_time = time.time() - (25 * 3600)  # 25 hours ago
        os.utime(old_marker, (old_time, old_time))
        
        # Create a recent marker
        new_marker = m.get_marker_path("new-job", 8001)
        new_marker.write_text("0")
        
        # Cleanup old markers
        removed = m.cleanup_stale(max_age_hours=24)
        
        assert removed >= 1
        assert not old_marker.exists()
        assert new_marker.exists()
        
        # Cleanup
        new_marker.unlink()


class TestEnvironmentVariables:
    """Test that environment variables are passed correctly."""
    
    def test_job_id_passed_to_wrapper(self):
        """Test that PROCCLAW_JOB_ID is available in job."""
        m = get_wrapper_manager()
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "env-test-1"
        env["PROCCLAW_RUN_ID"] = "9020"
        
        wrapped = m.wrap_command("echo $PROCCLAW_JOB_ID")
        result = subprocess.run(
            wrapped,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
        )
        
        assert "env-test-1" in result.stdout
        
        # Cleanup
        time.sleep(0.5)
        m.cleanup_marker("env-test-1", 9020)
    
    def test_run_id_passed_to_wrapper(self):
        """Test that PROCCLAW_RUN_ID is available in job."""
        m = get_wrapper_manager()
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "env-test-2"
        env["PROCCLAW_RUN_ID"] = "9021"
        
        wrapped = m.wrap_command("echo $PROCCLAW_RUN_ID")
        result = subprocess.run(
            wrapped,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
        )
        
        assert "9021" in result.stdout
        
        # Cleanup
        time.sleep(0.5)
        m.cleanup_marker("env-test-2", 9021)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
