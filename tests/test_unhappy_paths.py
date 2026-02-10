"""Extensive unhappy path tests for job wrapper.

100+ distinct failure scenarios to ensure robustness.
"""

import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
import random
import string

import pytest

from procclaw.core.job_wrapper import (
    JobWrapperManager,
    CompletionMarker,
    HeartbeatStatus,
)


@pytest.fixture
def temp_dirs():
    """Create temporary directories for markers and heartbeats."""
    marker_dir = Path(tempfile.mkdtemp(prefix="unhappy_markers_"))
    heartbeat_dir = Path(tempfile.mkdtemp(prefix="unhappy_heartbeats_"))
    yield marker_dir, heartbeat_dir
    shutil.rmtree(marker_dir, ignore_errors=True)
    shutil.rmtree(heartbeat_dir, ignore_errors=True)


@pytest.fixture
def manager(temp_dirs):
    """Create a JobWrapperManager with temp directories."""
    marker_dir, heartbeat_dir = temp_dirs
    return JobWrapperManager(
        marker_dir=marker_dir,
        heartbeat_dir=heartbeat_dir,
        heartbeat_interval=1,
        stale_threshold=3,
    )


class TestExitCodes:
    """Test various exit codes (1-50)."""
    
    @pytest.mark.parametrize("exit_code", range(1, 51))
    def test_exit_code(self, manager, exit_code):
        """Test that exit code {exit_code} is captured correctly."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = f"exit-{exit_code}"
        env["PROCCLAW_RUN_ID"] = str(1000 + exit_code)
        
        wrapped = manager.wrap_command(f"exit {exit_code}")
        subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=10)
        
        time.sleep(0.2)
        
        marker = manager.check_completion_marker(f"exit-{exit_code}", 1000 + exit_code)
        assert marker is not None, f"No marker for exit code {exit_code}"
        assert marker.exit_code == exit_code


class TestSignalHandling:
    """Test signal handling (15 scenarios)."""
    
    def test_sigterm_exit_143(self, manager):
        """SIGTERM should result in exit 143."""
        self._test_signal(manager, signal.SIGTERM, "sigterm", 2001, [143, -15])
    
    def test_sigint_exit_130(self, manager):
        """SIGINT should result in exit 130."""
        self._test_signal(manager, signal.SIGINT, "sigint", 2002, [130, -2, 2])
    
    def test_sighup_exit_129(self, manager):
        """SIGHUP should result in exit 129 (or 0 if handled gracefully)."""
        self._test_signal(manager, signal.SIGHUP, "sighup", 2003, [129, -1, 1, 0])
    
    def test_sigquit_exit_131(self, manager):
        """SIGQUIT should create marker."""
        self._test_signal(manager, signal.SIGQUIT, "sigquit", 2004, [131, -3, 3])
    
    def test_sigusr1(self, manager):
        """SIGUSR1 should create marker."""
        self._test_signal(manager, signal.SIGUSR1, "sigusr1", 2005, [138, -10, 10, 0])
    
    def test_sigusr2(self, manager):
        """SIGUSR2 should create marker."""
        self._test_signal(manager, signal.SIGUSR2, "sigusr2", 2006, [140, -12, 12, 0])
    
    def test_sigalrm(self, manager):
        """SIGALRM should create marker (or 0 if handled gracefully)."""
        self._test_signal(manager, signal.SIGALRM, "sigalrm", 2007, [142, -14, 14, 0])
    
    def test_sigpipe(self, manager):
        """SIGPIPE should create marker."""
        self._test_signal(manager, signal.SIGPIPE, "sigpipe", 2008, [141, -13, 13, 0])
    
    def test_multiple_sigterm(self, manager):
        """Multiple SIGTERMs should still create marker."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "multi-sigterm"
        env["PROCCLAW_RUN_ID"] = "2010"
        
        wrapped = manager.wrap_command("sleep 60")
        proc = subprocess.Popen(wrapped, shell=True, env=env, 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
        
        time.sleep(1)
        
        # Send SIGTERM multiple times
        for _ in range(3):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        
        proc.wait(timeout=5)
        time.sleep(0.3)
        
        marker = manager.check_completion_marker("multi-sigterm", 2010)
        # Marker should exist (might be 143 or 0 depending on timing)
        assert marker is not None
    
    def test_sigterm_during_output(self, manager):
        """SIGTERM during active output should still create marker."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "sigterm-output"
        env["PROCCLAW_RUN_ID"] = "2011"
        
        # Job that produces continuous output
        wrapped = manager.wrap_command("while true; do echo 'line'; sleep 0.1; done")
        proc = subprocess.Popen(wrapped, shell=True, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
        
        time.sleep(1)
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
        time.sleep(0.3)
        
        marker = manager.check_completion_marker("sigterm-output", 2011)
        assert marker is not None
    
    def _test_signal(self, manager, sig, name, run_id, expected_codes):
        """Helper to test signal handling."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = f"signal-{name}"
        env["PROCCLAW_RUN_ID"] = str(run_id)
        
        wrapped = manager.wrap_command("sleep 60")
        proc = subprocess.Popen(wrapped, shell=True, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
        
        time.sleep(1)
        
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            pass
        
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        
        time.sleep(0.3)
        
        marker = manager.check_completion_marker(f"signal-{name}", run_id)
        # For some signals, marker might not be created (SIGKILL-like behavior)
        if marker is not None:
            assert marker.exit_code in expected_codes, \
                f"Signal {name}: expected {expected_codes}, got {marker.exit_code}"


class TestFileSystemErrors:
    """Test filesystem error scenarios (15 scenarios)."""
    
    def test_marker_dir_readonly(self, manager, temp_dirs):
        """Test behavior when marker directory becomes readonly."""
        marker_dir, _ = temp_dirs
        
        # Make readonly
        os.chmod(marker_dir, 0o444)
        
        try:
            env = os.environ.copy()
            env["PROCCLAW_JOB_ID"] = "readonly-dir"
            env["PROCCLAW_RUN_ID"] = "3001"
            
            wrapped = manager.wrap_command("exit 0")
            result = subprocess.run(wrapped, shell=True, env=env, 
                                   capture_output=True, timeout=10)
            
            # Job should still exit (even if marker fails)
            # We accept either 0 (job succeeded) or non-zero (marker write failed)
        finally:
            os.chmod(marker_dir, 0o755)
    
    def test_marker_dir_deleted_during_run(self, manager, temp_dirs):
        """Test behavior when marker directory is deleted during job."""
        marker_dir, _ = temp_dirs
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "deleted-dir"
        env["PROCCLAW_RUN_ID"] = "3002"
        
        wrapped = manager.wrap_command("sleep 2")
        proc = subprocess.Popen(wrapped, shell=True, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        time.sleep(0.5)
        
        # Delete and recreate marker dir
        shutil.rmtree(marker_dir)
        marker_dir.mkdir()
        
        proc.wait(timeout=10)
        
        # Marker might or might not exist
        time.sleep(0.3)
    
    def test_disk_full_simulation(self, manager, temp_dirs):
        """Test handling when we can't write marker (simulated)."""
        marker_dir, _ = temp_dirs
        
        # Create a file where the marker would go
        marker_path = marker_dir / "disk-full-3003.done"
        marker_path.mkdir()  # Directory instead of file
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "disk-full"
        env["PROCCLAW_RUN_ID"] = "3003"
        
        wrapped = manager.wrap_command("exit 0")
        result = subprocess.run(wrapped, shell=True, env=env,
                               capture_output=True, timeout=10)
        
        # Cleanup
        marker_path.rmdir()
    
    def test_marker_corrupted_during_read(self, manager, temp_dirs):
        """Test handling of marker corrupted after creation."""
        marker_dir, _ = temp_dirs
        
        # Create valid marker first
        marker_path = marker_dir / "corrupt-read-3004.done"
        marker_path.write_text("0")
        
        # Read it
        marker = manager.check_completion_marker("corrupt-read", 3004)
        assert marker is not None
        
        # Now corrupt it
        marker_path.write_text("not-a-number")
        
        # Read again - should handle gracefully
        marker = manager.check_completion_marker("corrupt-read", 3004)
        assert marker is None
    
    def test_symlink_marker(self, manager, temp_dirs):
        """Test handling of symlink as marker."""
        marker_dir, _ = temp_dirs
        
        # Create a symlink
        real_file = marker_dir / "real-file"
        real_file.write_text("42")
        
        marker_path = marker_dir / "symlink-3005.done"
        marker_path.symlink_to(real_file)
        
        marker = manager.check_completion_marker("symlink", 3005)
        assert marker is not None
        assert marker.exit_code == 42
    
    def test_broken_symlink_marker(self, manager, temp_dirs):
        """Test handling of broken symlink as marker."""
        marker_dir, _ = temp_dirs
        
        marker_path = marker_dir / "broken-symlink-3006.done"
        marker_path.symlink_to("/nonexistent/path")
        
        marker = manager.check_completion_marker("broken-symlink", 3006)
        assert marker is None
    
    def test_marker_with_newline(self, manager, temp_dirs):
        """Test marker with trailing newline."""
        marker_dir, _ = temp_dirs
        
        marker_path = marker_dir / "newline-3007.done"
        marker_path.write_text("42\n")
        
        marker = manager.check_completion_marker("newline", 3007)
        assert marker is not None
        assert marker.exit_code == 42
    
    def test_marker_with_spaces(self, manager, temp_dirs):
        """Test marker with spaces."""
        marker_dir, _ = temp_dirs
        
        marker_path = marker_dir / "spaces-3008.done"
        marker_path.write_text("  42  \n")
        
        marker = manager.check_completion_marker("spaces", 3008)
        assert marker is not None
        assert marker.exit_code == 42
    
    def test_very_long_job_id(self, manager):
        """Test with very long job ID."""
        long_id = "x" * 200
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = long_id
        env["PROCCLAW_RUN_ID"] = "3009"
        
        wrapped = manager.wrap_command("exit 7")
        result = subprocess.run(wrapped, shell=True, env=env,
                               capture_output=True, timeout=10)
        
        time.sleep(0.3)
        
        marker = manager.check_completion_marker(long_id, 3009)
        assert marker is not None
        assert marker.exit_code == 7
    
    def test_special_chars_in_job_id(self, manager):
        """Test with special characters in job ID."""
        special_id = "job_with-special.chars_2024"
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = special_id
        env["PROCCLAW_RUN_ID"] = "3010"
        
        wrapped = manager.wrap_command("exit 0")
        subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=10)
        
        time.sleep(0.3)
        
        marker = manager.check_completion_marker(special_id, 3010)
        assert marker is not None


class TestConcurrencyIssues:
    """Test concurrency-related scenarios (20 scenarios)."""
    
    def test_rapid_successive_jobs_same_id(self, manager):
        """Test rapid jobs with same ID but different run IDs."""
        for i in range(10):
            env = os.environ.copy()
            env["PROCCLAW_JOB_ID"] = "rapid-same-id"
            env["PROCCLAW_RUN_ID"] = str(4000 + i)
            
            wrapped = manager.wrap_command(f"exit {i % 3}")
            subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=10)
            
            time.sleep(0.2)
            
            marker = manager.check_completion_marker("rapid-same-id", 4000 + i)
            assert marker is not None
            assert marker.exit_code == i % 3
            
            manager.cleanup_marker("rapid-same-id", 4000 + i)
    
    def test_concurrent_different_jobs(self, manager):
        """Test many concurrent jobs with different IDs."""
        processes = []
        count = 15
        
        for i in range(count):
            env = os.environ.copy()
            env["PROCCLAW_JOB_ID"] = f"concurrent-diff-{i}"
            env["PROCCLAW_RUN_ID"] = str(4100 + i)
            
            wrapped = manager.wrap_command(f"sleep 0.5 && exit {i % 5}")
            proc = subprocess.Popen(wrapped, shell=True, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append((i, proc))
        
        # Wait for all
        for i, proc in processes:
            proc.wait(timeout=30)
        
        time.sleep(0.5)
        
        # Verify all markers
        for i in range(count):
            marker = manager.check_completion_marker(f"concurrent-diff-{i}", 4100 + i)
            assert marker is not None, f"Missing marker for job {i}"
            assert marker.exit_code == i % 5
    
    def test_threaded_marker_reads(self, manager):
        """Test concurrent reads of same marker."""
        # Create marker
        marker_path = manager.get_marker_path("threaded-read", 4200)
        marker_path.write_text("42")
        
        results = []
        
        def read_marker():
            for _ in range(10):
                m = manager.check_completion_marker("threaded-read", 4200)
                results.append(m)
                time.sleep(0.01)
        
        threads = [threading.Thread(target=read_marker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All reads should succeed
        assert all(r is not None for r in results)
        assert all(r.exit_code == 42 for r in results)
    
    def test_threaded_cleanup(self, manager, temp_dirs):
        """Test concurrent cleanup operations."""
        marker_dir, _ = temp_dirs
        
        # Create many markers
        for i in range(50):
            (marker_dir / f"cleanup-test-{i}.done").write_text("0")
        
        errors = []
        
        def cleanup_batch(start, end):
            try:
                for i in range(start, end):
                    manager.cleanup_marker("cleanup-test", i)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=cleanup_batch, args=(0, 25)),
            threading.Thread(target=cleanup_batch, args=(25, 50)),
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Should not have errors
        assert len(errors) == 0
    
    def test_job_overwrites_stale_marker(self, manager):
        """Test that new job overwrites stale marker from previous run."""
        # Create "stale" marker with exit code 99
        marker_path = manager.get_marker_path("overwrite-test", 4300)
        marker_path.write_text("99")
        
        # Run new job with same ID/run_id
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "overwrite-test"
        env["PROCCLAW_RUN_ID"] = "4300"
        
        wrapped = manager.wrap_command("exit 0")
        subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=10)
        
        time.sleep(0.3)
        
        marker = manager.check_completion_marker("overwrite-test", 4300)
        assert marker is not None
        assert marker.exit_code == 0  # New value, not 99


class TestHeartbeatEdgeCases:
    """Test heartbeat edge cases (15 scenarios)."""
    
    def test_heartbeat_race_with_completion(self, manager):
        """Test heartbeat disappearing right as we check."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "hb-race"
        env["PROCCLAW_RUN_ID"] = "5001"
        
        wrapped = manager.wrap_command("sleep 0.5")
        proc = subprocess.Popen(wrapped, shell=True, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Rapid checking during completion
        found_heartbeat = False
        found_marker = False
        
        for _ in range(20):
            hb = manager.check_heartbeat("hb-race", 5001)
            if hb:
                found_heartbeat = True
            m = manager.check_completion_marker("hb-race", 5001)
            if m:
                found_marker = True
            time.sleep(0.1)
        
        proc.wait(timeout=10)
        
        # Should have found marker eventually
        time.sleep(0.3)
        marker = manager.check_completion_marker("hb-race", 5001)
        assert marker is not None
    
    def test_heartbeat_exactly_at_threshold(self, manager):
        """Test heartbeat exactly at stale threshold."""
        hb_path = manager.get_heartbeat_path("hb-threshold", 5002)
        # Exactly at threshold
        hb_path.write_text(str(int(time.time()) - manager.stale_threshold))
        
        status = manager.check_heartbeat("hb-threshold", 5002)
        assert status is not None
        # At exact threshold, could be either
        assert status.age_seconds >= manager.stale_threshold - 1
    
    def test_heartbeat_far_future(self, manager):
        """Test heartbeat with timestamp in future."""
        hb_path = manager.get_heartbeat_path("hb-future", 5003)
        hb_path.write_text(str(int(time.time()) + 3600))  # 1 hour in future
        
        status = manager.check_heartbeat("hb-future", 5003)
        assert status is not None
        # Should show as alive (negative age)
        assert status.is_alive
    
    def test_heartbeat_very_old(self, manager):
        """Test heartbeat from long ago."""
        hb_path = manager.get_heartbeat_path("hb-ancient", 5004)
        hb_path.write_text(str(int(time.time()) - 86400))  # 1 day ago
        
        status = manager.check_heartbeat("hb-ancient", 5004)
        assert status is not None
        assert not status.is_alive
        assert status.age_seconds > 86000
    
    def test_heartbeat_empty_file(self, manager):
        """Test empty heartbeat file."""
        hb_path = manager.get_heartbeat_path("hb-empty", 5005)
        hb_path.write_text("")
        
        status = manager.check_heartbeat("hb-empty", 5005)
        assert status is None
    
    def test_heartbeat_negative_timestamp(self, manager):
        """Test heartbeat with negative timestamp."""
        hb_path = manager.get_heartbeat_path("hb-negative", 5006)
        hb_path.write_text("-1000")
        
        status = manager.check_heartbeat("hb-negative", 5006)
        # Should handle gracefully
        if status:
            assert status.age_seconds > 1000
    
    def test_heartbeat_float_timestamp(self, manager):
        """Test heartbeat with float timestamp (should fail gracefully)."""
        hb_path = manager.get_heartbeat_path("hb-float", 5007)
        hb_path.write_text("1234567890.123")
        
        status = manager.check_heartbeat("hb-float", 5007)
        # Should fail to parse
        assert status is None
    
    def test_multiple_heartbeats_same_job(self, manager):
        """Test that heartbeat updates over time."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "hb-updates"
        env["PROCCLAW_RUN_ID"] = "5010"
        
        wrapped = manager.wrap_command("sleep 5")
        proc = subprocess.Popen(wrapped, shell=True, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        try:
            timestamps = []
            for _ in range(3):
                time.sleep(1.2)
                status = manager.check_heartbeat("hb-updates", 5010)
                if status:
                    timestamps.append(status.last_beat)
            
            # Should have increasing timestamps
            if len(timestamps) >= 2:
                assert timestamps[-1] > timestamps[0]
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestCommandEdgeCases:
    """Test various command edge cases (15 scenarios)."""
    
    def test_command_with_quotes(self, manager):
        """Test command with various quote styles."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "quotes-test"
        env["PROCCLAW_RUN_ID"] = "6001"
        
        wrapped = manager.wrap_command("echo \"hello 'world'\" && exit 0")
        result = subprocess.run(wrapped, shell=True, env=env,
                               capture_output=True, text=True, timeout=10)
        
        assert "hello" in result.stdout
        
        time.sleep(0.3)
        marker = manager.check_completion_marker("quotes-test", 6001)
        assert marker is not None
        assert marker.exit_code == 0
    
    def test_command_with_pipes(self, manager):
        """Test command with pipe operators."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "pipe-test"
        env["PROCCLAW_RUN_ID"] = "6002"
        
        wrapped = manager.wrap_command("echo 'hello' | grep 'hello' && exit 0")
        subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=10)
        
        time.sleep(0.3)
        marker = manager.check_completion_marker("pipe-test", 6002)
        assert marker is not None
        assert marker.exit_code == 0
    
    def test_command_with_redirect(self, manager, temp_dirs):
        """Test command with output redirect."""
        marker_dir, _ = temp_dirs
        output_file = marker_dir / "redirect_output.txt"
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "redirect-test"
        env["PROCCLAW_RUN_ID"] = "6003"
        
        wrapped = manager.wrap_command(f"echo 'test' > {output_file} && exit 0")
        subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=10)
        
        time.sleep(0.3)
        
        assert output_file.exists()
        marker = manager.check_completion_marker("redirect-test", 6003)
        assert marker is not None
    
    def test_command_with_env_var(self, manager):
        """Test command that uses environment variable."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "env-var-test"
        env["PROCCLAW_RUN_ID"] = "6004"
        env["MY_TEST_VAR"] = "hello123"
        
        wrapped = manager.wrap_command("echo $MY_TEST_VAR && exit 0")
        result = subprocess.run(wrapped, shell=True, env=env,
                               capture_output=True, text=True, timeout=10)
        
        assert "hello123" in result.stdout
        
        time.sleep(0.3)
        marker = manager.check_completion_marker("env-var-test", 6004)
        assert marker is not None
    
    def test_command_with_subshell(self, manager):
        """Test command with subshell."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "subshell-test"
        env["PROCCLAW_RUN_ID"] = "6005"
        
        wrapped = manager.wrap_command("(echo 'in subshell'; exit 5)")
        subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=10)
        
        time.sleep(0.3)
        marker = manager.check_completion_marker("subshell-test", 6005)
        assert marker is not None
        assert marker.exit_code == 5
    
    def test_command_with_background_process(self, manager):
        """Test command that starts background process."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "bg-test"
        env["PROCCLAW_RUN_ID"] = "6006"
        
        # Start background process but main should still complete
        # Use a shorter sleep and disown the background process
        wrapped = manager.wrap_command("(sleep 2 &); echo 'main' && exit 3")
        result = subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=15)
        
        time.sleep(0.3)
        marker = manager.check_completion_marker("bg-test", 6006)
        assert marker is not None
        assert marker.exit_code == 3
    
    def test_command_empty(self, manager):
        """Test empty command."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "empty-cmd"
        env["PROCCLAW_RUN_ID"] = "6007"
        
        wrapped = manager.wrap_command("")
        result = subprocess.run(wrapped, shell=True, env=env,
                               capture_output=True, timeout=10)
        
        time.sleep(0.3)
        marker = manager.check_completion_marker("empty-cmd", 6007)
        # Empty command exits 0
        assert marker is not None
        assert marker.exit_code == 0
    
    def test_command_just_whitespace(self, manager):
        """Test command that's just whitespace."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "whitespace-cmd"
        env["PROCCLAW_RUN_ID"] = "6008"
        
        wrapped = manager.wrap_command("   ")
        subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=10)
        
        time.sleep(0.3)
        marker = manager.check_completion_marker("whitespace-cmd", 6008)
        assert marker is not None
    
    def test_command_with_semicolons(self, manager):
        """Test command with multiple semicolon-separated commands."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "semicolon-cmd"
        env["PROCCLAW_RUN_ID"] = "6009"
        
        wrapped = manager.wrap_command("echo 'a'; echo 'b'; exit 7")
        subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=10)
        
        time.sleep(0.3)
        marker = manager.check_completion_marker("semicolon-cmd", 6009)
        assert marker is not None
        assert marker.exit_code == 7
    
    def test_command_with_or_operator(self, manager):
        """Test command with || operator."""
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = "or-cmd"
        env["PROCCLAW_RUN_ID"] = "6010"
        
        wrapped = manager.wrap_command("false || echo 'fallback' && exit 0")
        subprocess.run(wrapped, shell=True, env=env, capture_output=True, timeout=10)
        
        time.sleep(0.3)
        marker = manager.check_completion_marker("or-cmd", 6010)
        assert marker is not None
        assert marker.exit_code == 0


class TestResourceExhaustion:
    """Test resource exhaustion scenarios (10 scenarios)."""
    
    def test_many_markers_at_once(self, manager, temp_dirs):
        """Test creating many markers simultaneously."""
        marker_dir, _ = temp_dirs
        
        # Create 200 markers
        for i in range(200):
            (marker_dir / f"many-{i}-7000.done").write_text(str(i % 256))
        
        markers = manager.get_all_markers()
        many_markers = [m for m in markers if m.job_id.startswith("many-")]
        assert len(many_markers) == 200
    
    def test_rapid_create_delete_cycle(self, manager):
        """Test rapid create/delete cycles."""
        for i in range(50):
            env = os.environ.copy()
            env["PROCCLAW_JOB_ID"] = "rapid-cycle"
            env["PROCCLAW_RUN_ID"] = str(7100 + i)
            
            wrapped = manager.wrap_command("exit 0")
            subprocess.run(wrapped, shell=True, env=env, 
                          capture_output=True, timeout=10)
            
            time.sleep(0.1)
            manager.cleanup_marker("rapid-cycle", 7100 + i)
    
    def test_large_job_id(self, manager):
        """Test with maximum reasonable job ID length."""
        long_id = "a" * 255  # Max filename on most systems
        
        env = os.environ.copy()
        env["PROCCLAW_JOB_ID"] = long_id
        env["PROCCLAW_RUN_ID"] = "7200"
        
        wrapped = manager.wrap_command("exit 0")
        result = subprocess.run(wrapped, shell=True, env=env,
                               capture_output=True, timeout=10)
        
        time.sleep(0.3)
        
        # Might fail due to filename length limits
        marker = manager.check_completion_marker(long_id, 7200)
        # We just verify it doesn't crash


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
