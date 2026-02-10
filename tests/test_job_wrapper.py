"""Comprehensive tests for job wrapper functionality.

Tests cover:
- Happy path: normal job completion
- Crash scenarios: daemon dies, job dies, both die
- Edge cases: concurrent jobs, rapid restarts, filesystem issues
- Stress tests: many jobs, rapid heartbeats
"""

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from procclaw.core.job_wrapper import (
    JobWrapperManager,
    CompletionMarker,
    HeartbeatStatus,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_STALE_THRESHOLD,
)


@pytest.fixture
def temp_dirs():
    """Create temporary directories for markers and heartbeats."""
    marker_dir = Path(tempfile.mkdtemp(prefix="procclaw_markers_"))
    heartbeat_dir = Path(tempfile.mkdtemp(prefix="procclaw_heartbeats_"))
    yield marker_dir, heartbeat_dir
    # Cleanup
    shutil.rmtree(marker_dir, ignore_errors=True)
    shutil.rmtree(heartbeat_dir, ignore_errors=True)


@pytest.fixture
def manager(temp_dirs):
    """Create a JobWrapperManager with temp directories."""
    marker_dir, heartbeat_dir = temp_dirs
    return JobWrapperManager(
        marker_dir=marker_dir,
        heartbeat_dir=heartbeat_dir,
        heartbeat_interval=1,  # Fast for testing
        stale_threshold=3,     # Fast for testing
    )


class TestJobWrapperManager:
    """Tests for JobWrapperManager initialization and basic operations."""
    
    def test_init_creates_directories(self, temp_dirs):
        """Test that initialization creates necessary directories."""
        marker_dir, heartbeat_dir = temp_dirs
        # Remove dirs first
        shutil.rmtree(marker_dir)
        shutil.rmtree(heartbeat_dir)
        
        manager = JobWrapperManager(
            marker_dir=marker_dir,
            heartbeat_dir=heartbeat_dir,
        )
        
        assert marker_dir.exists()
        assert heartbeat_dir.exists()
    
    def test_init_creates_wrapper_script(self, manager, temp_dirs):
        """Test that initialization creates the wrapper script."""
        marker_dir, _ = temp_dirs
        wrapper_path = marker_dir / "procclaw-wrapper.sh"
        
        assert wrapper_path.exists()
        assert os.access(wrapper_path, os.X_OK)
    
    def test_wrapper_script_is_valid_bash(self, manager):
        """Test that the wrapper script is valid bash syntax."""
        result = subprocess.run(
            ["bash", "-n", str(manager.wrapper_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error: {result.stderr}"
    
    def test_wrap_command(self, manager):
        """Test command wrapping."""
        cmd = "echo hello world"
        wrapped = manager.wrap_command(cmd)
        
        assert str(manager.wrapper_path) in wrapped
        assert cmd in wrapped
    
    def test_get_marker_path(self, manager, temp_dirs):
        """Test marker path generation."""
        marker_dir, _ = temp_dirs
        path = manager.get_marker_path("test-job", 123)
        
        assert path == marker_dir / "test-job-123.done"
    
    def test_get_heartbeat_path(self, manager, temp_dirs):
        """Test heartbeat path generation."""
        _, heartbeat_dir = temp_dirs
        path = manager.get_heartbeat_path("test-job", 123)
        
        assert path == heartbeat_dir / "test-job-123"


class TestCompletionMarkers:
    """Tests for completion marker functionality."""
    
    def test_check_nonexistent_marker(self, manager):
        """Test checking a marker that doesn't exist."""
        result = manager.check_completion_marker("nonexistent", 999)
        assert result is None
    
    def test_check_valid_marker(self, manager):
        """Test checking a valid marker."""
        marker_path = manager.get_marker_path("test-job", 1)
        marker_path.write_text("0")
        
        result = manager.check_completion_marker("test-job", 1)
        
        assert result is not None
        assert result.job_id == "test-job"
        assert result.run_id == 1
        assert result.exit_code == 0
    
    def test_check_marker_with_error_exit_code(self, manager):
        """Test marker with non-zero exit code."""
        marker_path = manager.get_marker_path("failed-job", 2)
        marker_path.write_text("1")
        
        result = manager.check_completion_marker("failed-job", 2)
        
        assert result.exit_code == 1
    
    def test_check_marker_with_signal_exit_code(self, manager):
        """Test marker with signal exit code (128+signal)."""
        marker_path = manager.get_marker_path("killed-job", 3)
        marker_path.write_text("143")  # SIGTERM
        
        result = manager.check_completion_marker("killed-job", 3)
        
        assert result.exit_code == 143
    
    def test_check_marker_with_negative_exit_code(self, manager):
        """Test marker with negative exit code."""
        marker_path = manager.get_marker_path("neg-job", 4)
        marker_path.write_text("-15")
        
        result = manager.check_completion_marker("neg-job", 4)
        
        assert result.exit_code == -15
    
    def test_check_corrupted_marker(self, manager):
        """Test handling of corrupted marker file."""
        marker_path = manager.get_marker_path("corrupt-job", 5)
        marker_path.write_text("not_a_number")
        
        result = manager.check_completion_marker("corrupt-job", 5)
        
        assert result is None
    
    def test_check_empty_marker(self, manager):
        """Test handling of empty marker file."""
        marker_path = manager.get_marker_path("empty-job", 6)
        marker_path.write_text("")
        
        result = manager.check_completion_marker("empty-job", 6)
        
        assert result is None
    
    def test_cleanup_marker(self, manager):
        """Test marker cleanup."""
        marker_path = manager.get_marker_path("cleanup-job", 7)
        marker_path.write_text("0")
        
        assert marker_path.exists()
        result = manager.cleanup_marker("cleanup-job", 7)
        
        assert result is True
        assert not marker_path.exists()
    
    def test_cleanup_nonexistent_marker(self, manager):
        """Test cleanup of marker that doesn't exist."""
        result = manager.cleanup_marker("nonexistent", 999)
        assert result is False
    
    def test_get_all_markers(self, manager):
        """Test getting all markers."""
        # Create multiple markers
        manager.get_marker_path("job-a", 1).write_text("0")
        manager.get_marker_path("job-b", 2).write_text("1")
        manager.get_marker_path("job-c", 3).write_text("143")
        
        markers = manager.get_all_markers()
        
        assert len(markers) == 3
        job_ids = {m.job_id for m in markers}
        assert job_ids == {"job-a", "job-b", "job-c"}


class TestHeartbeats:
    """Tests for heartbeat functionality."""
    
    def test_check_nonexistent_heartbeat(self, manager):
        """Test checking heartbeat that doesn't exist."""
        result = manager.check_heartbeat("nonexistent", 999)
        assert result is None
    
    def test_check_fresh_heartbeat(self, manager):
        """Test checking a fresh heartbeat (alive)."""
        hb_path = manager.get_heartbeat_path("alive-job", 1)
        hb_path.write_text(str(int(time.time())))
        
        result = manager.check_heartbeat("alive-job", 1)
        
        assert result is not None
        assert result.job_id == "alive-job"
        assert result.run_id == 1
        assert result.is_alive is True
        assert result.age_seconds < 2
    
    def test_check_stale_heartbeat(self, manager):
        """Test checking a stale heartbeat (dead)."""
        hb_path = manager.get_heartbeat_path("dead-job", 2)
        old_time = int(time.time()) - 60  # 1 minute ago
        hb_path.write_text(str(old_time))
        
        result = manager.check_heartbeat("dead-job", 2)
        
        assert result is not None
        assert result.is_alive is False
        assert result.age_seconds > 50
    
    def test_check_corrupted_heartbeat(self, manager):
        """Test handling of corrupted heartbeat file."""
        hb_path = manager.get_heartbeat_path("corrupt-job", 3)
        hb_path.write_text("not_a_timestamp")
        
        result = manager.check_heartbeat("corrupt-job", 3)
        
        assert result is None
    
    def test_cleanup_heartbeat(self, manager):
        """Test heartbeat cleanup."""
        hb_path = manager.get_heartbeat_path("cleanup-job", 4)
        hb_path.write_text(str(int(time.time())))
        
        assert hb_path.exists()
        result = manager.cleanup_heartbeat("cleanup-job", 4)
        
        assert result is True
        assert not hb_path.exists()
    
    def test_get_all_heartbeats(self, manager):
        """Test getting all heartbeats."""
        now = int(time.time())
        manager.get_heartbeat_path("job-x", 10).write_text(str(now))
        manager.get_heartbeat_path("job-y", 20).write_text(str(now - 60))
        
        heartbeats = manager.get_all_heartbeats()
        
        assert len(heartbeats) == 2
        alive_count = sum(1 for h in heartbeats if h.is_alive)
        dead_count = sum(1 for h in heartbeats if not h.is_alive)
        assert alive_count == 1
        assert dead_count == 1


class TestWrapperExecution:
    """Integration tests for actual wrapper script execution."""
    
    def test_successful_job_creates_marker(self, manager):
        """Test that successful job creates completion marker."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "success-job"
        env["PROCCLAW_RUN_ID"] = "100"
        
        wrapped_cmd = manager.wrap_command("exit 0")
        result = subprocess.run(
            wrapped_cmd,
            shell=True,
            env=env,
            capture_output=True,
            timeout=10,
        )
        
        # Give a moment for cleanup to run
        time.sleep(0.2)
        
        marker = manager.check_completion_marker("success-job", 100)
        assert marker is not None
        assert marker.exit_code == 0
    
    def test_failed_job_creates_marker(self, manager):
        """Test that failed job creates completion marker."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "fail-job"
        env["PROCCLAW_RUN_ID"] = "101"
        
        wrapped_cmd = manager.wrap_command("exit 42")
        result = subprocess.run(
            wrapped_cmd,
            shell=True,
            env=env,
            capture_output=True,
            timeout=10,
        )
        
        time.sleep(0.2)
        
        marker = manager.check_completion_marker("fail-job", 101)
        assert marker is not None
        assert marker.exit_code == 42
    
    def test_job_produces_heartbeat(self, manager):
        """Test that running job produces heartbeat."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "heartbeat-job"
        env["PROCCLAW_RUN_ID"] = "102"
        
        # Start a long-running job
        wrapped_cmd = manager.wrap_command("sleep 5")
        proc = subprocess.Popen(
            wrapped_cmd,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        
        try:
            # Wait for heartbeat to appear
            time.sleep(2)
            
            status = manager.check_heartbeat("heartbeat-job", 102)
            assert status is not None
            assert status.is_alive is True
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    
    def test_heartbeat_stops_after_job_completes(self, manager):
        """Test that heartbeat file is cleaned up after job completes."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "cleanup-hb-job"
        env["PROCCLAW_RUN_ID"] = "103"
        
        wrapped_cmd = manager.wrap_command("sleep 0.5")
        result = subprocess.run(
            wrapped_cmd,
            shell=True,
            env=env,
            capture_output=True,
            timeout=10,
        )
        
        time.sleep(0.3)
        
        # Heartbeat should be gone
        status = manager.check_heartbeat("cleanup-hb-job", 103)
        assert status is None
        
        # But marker should exist
        marker = manager.check_completion_marker("cleanup-hb-job", 103)
        assert marker is not None
    
    def test_sigterm_creates_marker(self, manager):
        """Test that SIGTERM'd job still creates marker."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "term-job"
        env["PROCCLAW_RUN_ID"] = "104"
        
        wrapped_cmd = manager.wrap_command("sleep 60")
        proc = subprocess.Popen(
            wrapped_cmd,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        
        time.sleep(1)  # Let it start
        
        # Send SIGTERM to process group
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
        
        time.sleep(0.3)
        
        marker = manager.check_completion_marker("term-job", 104)
        assert marker is not None
        # Exit code should be 143 (128 + SIGTERM=15) or 130 depending on shell
        assert marker.exit_code in (143, 130, -15)
    
    def test_sigint_creates_marker(self, manager):
        """Test that SIGINT'd job still creates marker."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "int-job"
        env["PROCCLAW_RUN_ID"] = "105"
        
        wrapped_cmd = manager.wrap_command("sleep 60")
        proc = subprocess.Popen(
            wrapped_cmd,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        
        time.sleep(1)
        
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        proc.wait(timeout=5)
        
        time.sleep(0.3)
        
        marker = manager.check_completion_marker("int-job", 105)
        assert marker is not None
        assert marker.exit_code in (130, 2, -2)  # SIGINT handling varies
    
    def test_job_with_output(self, manager, temp_dirs):
        """Test job that produces stdout/stderr."""
        marker_dir, _ = temp_dirs
        output_file = marker_dir / "test_output.txt"
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "output-job"
        env["PROCCLAW_RUN_ID"] = "106"
        
        cmd = f"echo 'hello world' && echo 'error msg' >&2"
        wrapped_cmd = manager.wrap_command(cmd)
        result = subprocess.run(
            wrapped_cmd,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        
        assert "hello world" in result.stdout
        assert "error msg" in result.stderr
        
        marker = manager.check_completion_marker("output-job", 106)
        assert marker is not None
        assert marker.exit_code == 0
    
    def test_job_with_arguments(self, manager):
        """Test job command with complex arguments."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "args-job"
        env["PROCCLAW_RUN_ID"] = "107"
        
        # Command with quotes, spaces, special chars
        cmd = """bash -c 'echo "hello world" && echo $HOME'"""
        wrapped_cmd = manager.wrap_command(cmd)
        result = subprocess.run(
            wrapped_cmd,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        
        assert "hello world" in result.stdout
        
        marker = manager.check_completion_marker("args-job", 107)
        assert marker is not None


class TestEdgeCases:
    """Tests for edge cases and error conditions."""
    
    def test_job_id_with_special_chars(self, manager):
        """Test job ID with special characters."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "my_job-2024.01.01"
        env["PROCCLAW_RUN_ID"] = "200"
        
        wrapped_cmd = manager.wrap_command("exit 0")
        subprocess.run(wrapped_cmd, shell=True, env=env, timeout=10)
        
        time.sleep(0.2)
        
        marker = manager.check_completion_marker("my_job-2024.01.01", 200)
        assert marker is not None
    
    def test_missing_env_vars(self, manager):
        """Test wrapper behavior when env vars are missing."""
        # Don't set PROCCLAW_JOB_ID or PROCCLAW_RUN_ID
        wrapped_cmd = manager.wrap_command("exit 0")
        result = subprocess.run(
            wrapped_cmd,
            shell=True,
            capture_output=True,
            timeout=10,
        )
        
        # Should still complete (uses defaults)
        assert result.returncode == 0
    
    def test_concurrent_jobs(self, manager):
        """Test multiple concurrent jobs."""
        processes = []
        
        for i in range(5):
            env = os.environ.copy()
            env["PROCCLAW_JOB_ID"] = f"concurrent-job-{i}"
            env["PROCCLAW_RUN_ID"] = str(300 + i)
            
            wrapped_cmd = manager.wrap_command(f"sleep 1 && exit {i}")
            proc = subprocess.Popen(
                wrapped_cmd,
                shell=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append((i, proc))
        
        # Wait for all
        for i, proc in processes:
            proc.wait(timeout=10)
        
        time.sleep(0.3)
        
        # Check all markers
        for i in range(5):
            marker = manager.check_completion_marker(f"concurrent-job-{i}", 300 + i)
            assert marker is not None, f"Missing marker for job {i}"
            assert marker.exit_code == i
    
    def test_rapid_job_sequence(self, manager):
        """Test rapid sequential job starts."""
        for i in range(10):
            env = os.environ.copy()
            env["PROCCLAW_JOB_ID"] = "rapid-job"
            env["PROCCLAW_RUN_ID"] = str(400 + i)
            
            wrapped_cmd = manager.wrap_command("exit 0")
            result = subprocess.run(
                wrapped_cmd,
                shell=True,
                env=env,
                timeout=10,
            )
            
            time.sleep(0.1)
            
            marker = manager.check_completion_marker("rapid-job", 400 + i)
            assert marker is not None
            manager.cleanup_marker("rapid-job", 400 + i)
    
    def test_long_running_job_heartbeat_updates(self, manager):
        """Test that heartbeat updates over time."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "long-job"
        env["PROCCLAW_RUN_ID"] = "500"
        
        wrapped_cmd = manager.wrap_command("sleep 5")
        proc = subprocess.Popen(
            wrapped_cmd,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        
        try:
            time.sleep(1.5)
            status1 = manager.check_heartbeat("long-job", 500)
            
            time.sleep(2)
            status2 = manager.check_heartbeat("long-job", 500)
            
            assert status1 is not None
            assert status2 is not None
            # Second heartbeat should be newer
            assert status2.last_beat > status1.last_beat
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    
    def test_cleanup_stale_files(self, manager, temp_dirs):
        """Test cleanup of old marker files."""
        marker_dir, heartbeat_dir = temp_dirs
        
        # Create some old files
        old_marker = marker_dir / "old-job-999.done"
        old_marker.write_text("0")
        old_time = time.time() - (25 * 3600)  # 25 hours ago
        os.utime(old_marker, (old_time, old_time))
        
        # Create a recent file
        new_marker = marker_dir / "new-job-998.done"
        new_marker.write_text("0")
        
        removed = manager.cleanup_stale(max_age_hours=24)
        
        assert removed >= 1
        assert not old_marker.exists()
        assert new_marker.exists()


class TestFailureScenarios:
    """Tests for various failure scenarios."""
    
    def test_job_crashes_no_marker(self, manager):
        """Test detecting job that crashed without marker (SIGKILL)."""
        # This simulates what happens when SIGKILL is received
        hb_path = manager.get_heartbeat_path("crash-job", 600)
        old_time = int(time.time()) - 60
        hb_path.write_text(str(old_time))
        
        # No marker, stale heartbeat = job died
        marker = manager.check_completion_marker("crash-job", 600)
        status = manager.check_heartbeat("crash-job", 600)
        
        assert marker is None
        assert status is not None
        assert status.is_alive is False
    
    def test_job_completes_after_daemon_restart(self, manager):
        """Simulate: job completes, daemon finds marker on restart."""
        # Write marker directly (simulates job that completed while daemon was down)
        marker_path = manager.get_marker_path("orphan-job", 601)
        marker_path.write_text("0")
        
        # Daemon "restarts" and checks
        marker = manager.check_completion_marker("orphan-job", 601)
        
        assert marker is not None
        assert marker.exit_code == 0
    
    def test_job_fails_after_daemon_restart(self, manager):
        """Simulate: job fails, daemon finds marker on restart."""
        marker_path = manager.get_marker_path("fail-orphan-job", 602)
        marker_path.write_text("1")
        
        marker = manager.check_completion_marker("fail-orphan-job", 602)
        
        assert marker is not None
        assert marker.exit_code == 1
    
    def test_filesystem_permission_error(self, manager, temp_dirs):
        """Test handling of filesystem permission errors."""
        marker_dir, _ = temp_dirs
        
        # Create unreadable marker
        bad_marker = marker_dir / "bad-job-700.done"
        bad_marker.write_text("0")
        bad_marker.chmod(0o000)
        
        try:
            # Should not crash, just return None
            marker = manager.check_completion_marker("bad-job", 700)
            # May or may not be None depending on user (root can read anything)
        finally:
            bad_marker.chmod(0o644)
    
    def test_marker_directory_deleted(self, manager, temp_dirs):
        """Test behavior when marker directory is deleted."""
        marker_dir, _ = temp_dirs
        
        # Write a marker
        marker_path = manager.get_marker_path("test-job", 800)
        marker_path.write_text("0")
        
        # Delete the directory
        shutil.rmtree(marker_dir)
        
        # Should not crash
        marker = manager.check_completion_marker("test-job", 800)
        assert marker is None


class TestStressAndPerformance:
    """Stress tests and performance checks."""
    
    def test_many_concurrent_jobs(self, manager):
        """Test 20 concurrent jobs."""
        processes = []
        count = 20
        
        for i in range(count):
            env = os.environ.copy()
            env["PROCCLAW_JOB_ID"] = f"stress-job-{i}"
            env["PROCCLAW_RUN_ID"] = str(900 + i)
            
            wrapped_cmd = manager.wrap_command(f"sleep 0.5 && exit {i % 3}")
            proc = subprocess.Popen(
                wrapped_cmd,
                shell=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(proc)
        
        # Wait for all
        for proc in processes:
            proc.wait(timeout=30)
        
        time.sleep(0.5)
        
        # Check all completed
        markers = manager.get_all_markers()
        stress_markers = [m for m in markers if m.job_id.startswith("stress-job-")]
        assert len(stress_markers) == count
    
    def test_get_all_markers_performance(self, manager):
        """Test performance of get_all_markers with many files."""
        # Create 100 markers
        for i in range(100):
            path = manager.get_marker_path(f"perf-job-{i}", 1000 + i)
            path.write_text(str(i % 256))
        
        start = time.time()
        markers = manager.get_all_markers()
        elapsed = time.time() - start
        
        assert len(markers) >= 100
        assert elapsed < 1.0  # Should be fast
    
    def test_rapid_heartbeat_checks(self, manager):
        """Test rapid heartbeat checking."""
        # Create a heartbeat
        hb_path = manager.get_heartbeat_path("rapid-hb-job", 1100)
        hb_path.write_text(str(int(time.time())))
        
        # Rapid checks
        for _ in range(100):
            status = manager.check_heartbeat("rapid-hb-job", 1100)
            assert status is not None


# Additional scenario tests
class TestRecoveryScenarios:
    """Tests for daemon restart/recovery scenarios."""
    
    def test_scenario_daemon_dies_job_completes(self, manager):
        """
        Scenario:
        1. Job starts, daemon tracks it
        2. Daemon dies (simulated)
        3. Job completes normally
        4. Daemon restarts, finds marker
        """
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "scenario-1"
        env["PROCCLAW_RUN_ID"] = "1001"
        
        # Start job
        wrapped_cmd = manager.wrap_command("sleep 1 && exit 0")
        proc = subprocess.Popen(
            wrapped_cmd,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        
        # "Daemon dies" - we just stop tracking
        # Job continues running...
        
        # Wait for job to complete
        proc.wait(timeout=10)
        time.sleep(0.3)
        
        # "Daemon restarts" - check for markers
        marker = manager.check_completion_marker("scenario-1", 1001)
        
        assert marker is not None
        assert marker.exit_code == 0
    
    def test_scenario_daemon_dies_job_fails(self, manager):
        """
        Scenario:
        1. Job starts
        2. Daemon dies
        3. Job fails with error
        4. Daemon restarts, finds marker with error code
        """
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "scenario-2"
        env["PROCCLAW_RUN_ID"] = "1002"
        
        wrapped_cmd = manager.wrap_command("sleep 0.5 && exit 5")
        result = subprocess.run(
            wrapped_cmd,
            shell=True,
            env=env,
            timeout=10,
        )
        
        time.sleep(0.3)
        
        marker = manager.check_completion_marker("scenario-2", 1002)
        
        assert marker is not None
        assert marker.exit_code == 5
    
    def test_scenario_daemon_dies_job_still_running(self, manager):
        """
        Scenario:
        1. Job starts
        2. Daemon dies
        3. Job still running
        4. Daemon restarts, checks heartbeat (should be alive)
        """
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "scenario-3"
        env["PROCCLAW_RUN_ID"] = "1003"
        
        wrapped_cmd = manager.wrap_command("sleep 30")
        proc = subprocess.Popen(
            wrapped_cmd,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        
        try:
            time.sleep(2)
            
            # "Daemon restarts" - checks state
            marker = manager.check_completion_marker("scenario-3", 1003)
            status = manager.check_heartbeat("scenario-3", 1003)
            
            assert marker is None  # Not completed yet
            assert status is not None
            assert status.is_alive is True
        finally:
            # Kill the whole process group
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
    
    def test_scenario_job_sigkilled_no_marker(self, manager):
        """
        Scenario:
        1. Job starts
        2. Job receives SIGKILL (immediate death, no cleanup)
        3. Daemon checks - no marker, stale heartbeat
        """
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "scenario-4"
        env["PROCCLAW_RUN_ID"] = "1004"
        
        wrapped_cmd = manager.wrap_command("sleep 60")
        proc = subprocess.Popen(
            wrapped_cmd,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        
        time.sleep(1.5)  # Let heartbeat start
        
        # SIGKILL - no chance to cleanup
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=5)
        
        time.sleep(0.3)
        
        # Marker might or might not exist (depends on shell handling)
        # But heartbeat should be stale after threshold
        time.sleep(manager.stale_threshold + 1)
        
        status = manager.check_heartbeat("scenario-4", 1004)
        # Heartbeat file might be cleaned up or stale
        if status is not None:
            assert status.is_alive is False
    
    def test_scenario_multiple_daemon_restarts(self, manager):
        """
        Scenario:
        1. Job starts (run 1)
        2. Daemon restarts, finds marker for run 1
        3. Job starts again (run 2)
        4. Daemon restarts, finds marker for run 2
        """
        # Run 1
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "scenario-5"
        env["PROCCLAW_RUN_ID"] = "1005"
        
        subprocess.run(
            manager.wrap_command("exit 0"),
            shell=True,
            env=env,
            timeout=10,
        )
        time.sleep(0.2)
        
        marker1 = manager.check_completion_marker("scenario-5", 1005)
        assert marker1 is not None
        manager.cleanup_marker("scenario-5", 1005)
        
        # Run 2
        env["PROCCLAW_RUN_ID"] = "1006"
        subprocess.run(
            manager.wrap_command("exit 1"),
            shell=True,
            env=env,
            timeout=10,
        )
        time.sleep(0.2)
        
        marker2 = manager.check_completion_marker("scenario-5", 1006)
        assert marker2 is not None
        assert marker2.exit_code == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
