"""Job wrapper for reliable completion tracking.

This module provides:
1. A wrapper script that jobs run inside
2. Heartbeat mechanism to detect if job is alive
3. Completion markers to capture exit codes even if daemon dies

The wrapper is transparent to jobs - they don't need any modifications.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from loguru import logger


# Default directories
DEFAULT_MARKER_DIR = Path("/tmp/procclaw-markers")
DEFAULT_HEARTBEAT_DIR = Path("/tmp/procclaw-heartbeats")

# Heartbeat settings
HEARTBEAT_INTERVAL = 5  # seconds
HEARTBEAT_STALE_THRESHOLD = 15  # seconds - if older than this, job is dead


class CompletionMarker(NamedTuple):
    """Represents a job completion marker."""
    job_id: str
    run_id: int
    exit_code: int
    timestamp: datetime


class HeartbeatStatus(NamedTuple):
    """Represents the status of a job's heartbeat."""
    job_id: str
    run_id: int
    last_beat: datetime
    age_seconds: float
    is_alive: bool


class JobWrapperManager:
    """Manages job wrapper, heartbeats, and completion markers."""
    
    def __init__(
        self,
        marker_dir: Path | None = None,
        heartbeat_dir: Path | None = None,
        heartbeat_interval: int = HEARTBEAT_INTERVAL,
        stale_threshold: int = HEARTBEAT_STALE_THRESHOLD,
    ):
        self.marker_dir = marker_dir or DEFAULT_MARKER_DIR
        self.heartbeat_dir = heartbeat_dir or DEFAULT_HEARTBEAT_DIR
        self.heartbeat_interval = heartbeat_interval
        self.stale_threshold = stale_threshold
        
        # Ensure directories exist
        self.marker_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_dir.mkdir(parents=True, exist_ok=True)
        
        # Create wrapper script
        self._wrapper_path = self.marker_dir / "procclaw-wrapper.sh"
        self._create_wrapper_script()
    
    def _create_wrapper_script(self) -> None:
        """Create the wrapper shell script."""
        script = f'''#!/bin/bash
# ProcClaw Job Wrapper
# Provides heartbeat and completion marker functionality

set -o pipefail

JOB_ID="${{PROCCLAW_JOB_ID:-unknown}}"
RUN_ID="${{PROCCLAW_RUN_ID:-0}}"
HEARTBEAT_FILE="{self.heartbeat_dir}/${{JOB_ID}}-${{RUN_ID}}"
MARKER_FILE="{self.marker_dir}/${{JOB_ID}}-${{RUN_ID}}.done"
HEARTBEAT_INTERVAL={self.heartbeat_interval}

# Cleanup function
cleanup() {{
    local exit_code=$?
    
    # Stop heartbeat
    if [ -n "$HEARTBEAT_PID" ] && kill -0 "$HEARTBEAT_PID" 2>/dev/null; then
        kill "$HEARTBEAT_PID" 2>/dev/null
        wait "$HEARTBEAT_PID" 2>/dev/null
    fi
    
    # Remove heartbeat file
    rm -f "$HEARTBEAT_FILE"
    
    # Write completion marker (ALWAYS, even on signal)
    echo "$exit_code" > "$MARKER_FILE"
    
    exit $exit_code
}}

# Set up signal handlers
trap cleanup EXIT
trap 'exit 130' INT   # Ctrl+C
trap 'exit 143' TERM  # kill

# Start heartbeat in background
(
    while true; do
        date +%s > "$HEARTBEAT_FILE"
        sleep $HEARTBEAT_INTERVAL
    done
) &
HEARTBEAT_PID=$!

# Execute the actual command
"$@"
EXIT_CODE=$?

# Cleanup will be called via trap
exit $EXIT_CODE
'''
        
        self._wrapper_path.write_text(script)
        self._wrapper_path.chmod(self._wrapper_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        logger.debug(f"Created wrapper script at {self._wrapper_path}")
    
    @property
    def wrapper_path(self) -> Path:
        """Get the path to the wrapper script."""
        return self._wrapper_path
    
    def wrap_command(self, cmd: str) -> str:
        """Wrap a command to use the wrapper script.
        
        The command is passed to bash -c to ensure proper handling of
        shell operators like && || ; etc.
        """
        # Escape single quotes in the command
        escaped_cmd = cmd.replace("'", "'\\''")
        return f'"{self._wrapper_path}" bash -c \'{escaped_cmd}\''
    
    def get_marker_path(self, job_id: str, run_id: int) -> Path:
        """Get the path to a completion marker file."""
        return self.marker_dir / f"{job_id}-{run_id}.done"
    
    def get_heartbeat_path(self, job_id: str, run_id: int) -> Path:
        """Get the path to a heartbeat file."""
        return self.heartbeat_dir / f"{job_id}-{run_id}"
    
    def check_completion_marker(self, job_id: str, run_id: int) -> CompletionMarker | None:
        """Check if a completion marker exists for a job.
        
        Returns:
            CompletionMarker if found, None otherwise
        """
        marker_path = self.get_marker_path(job_id, run_id)
        
        if not marker_path.exists():
            return None
        
        try:
            content = marker_path.read_text().strip()
            exit_code = int(content)
            mtime = datetime.fromtimestamp(marker_path.stat().st_mtime)
            
            return CompletionMarker(
                job_id=job_id,
                run_id=run_id,
                exit_code=exit_code,
                timestamp=mtime,
            )
        except (ValueError, OSError) as e:
            logger.warning(f"Failed to read completion marker for {job_id}-{run_id}: {e}")
            return None
    
    def check_heartbeat(self, job_id: str, run_id: int) -> HeartbeatStatus | None:
        """Check the heartbeat status of a job.
        
        Returns:
            HeartbeatStatus if heartbeat file exists, None otherwise
        """
        hb_path = self.get_heartbeat_path(job_id, run_id)
        
        if not hb_path.exists():
            return None
        
        try:
            content = hb_path.read_text().strip()
            last_beat_ts = int(content)
            last_beat = datetime.fromtimestamp(last_beat_ts)
            age = time.time() - last_beat_ts
            
            return HeartbeatStatus(
                job_id=job_id,
                run_id=run_id,
                last_beat=last_beat,
                age_seconds=age,
                is_alive=age < self.stale_threshold,
            )
        except (ValueError, OSError) as e:
            logger.warning(f"Failed to read heartbeat for {job_id}-{run_id}: {e}")
            return None
    
    def cleanup_marker(self, job_id: str, run_id: int) -> bool:
        """Remove a completion marker file.
        
        Returns:
            True if marker was removed, False otherwise
        """
        marker_path = self.get_marker_path(job_id, run_id)
        try:
            if marker_path.exists():
                marker_path.unlink()
                return True
        except OSError as e:
            logger.warning(f"Failed to remove marker {marker_path}: {e}")
        return False
    
    def cleanup_heartbeat(self, job_id: str, run_id: int) -> bool:
        """Remove a heartbeat file.
        
        Returns:
            True if heartbeat was removed, False otherwise
        """
        hb_path = self.get_heartbeat_path(job_id, run_id)
        try:
            if hb_path.exists():
                hb_path.unlink()
                return True
        except OSError as e:
            logger.warning(f"Failed to remove heartbeat {hb_path}: {e}")
        return False
    
    def get_all_markers(self) -> list[CompletionMarker]:
        """Get all completion markers.
        
        Returns:
            List of all CompletionMarkers found
        """
        markers = []
        for path in self.marker_dir.glob("*.done"):
            try:
                # Parse filename: {job_id}-{run_id}.done
                stem = path.stem  # removes .done
                parts = stem.rsplit("-", 1)
                if len(parts) == 2:
                    job_id, run_id_str = parts
                    run_id = int(run_id_str)
                    marker = self.check_completion_marker(job_id, run_id)
                    if marker:
                        markers.append(marker)
            except (ValueError, IndexError):
                logger.warning(f"Invalid marker filename: {path.name}")
        return markers
    
    def get_all_heartbeats(self) -> list[HeartbeatStatus]:
        """Get all heartbeat statuses.
        
        Returns:
            List of all HeartbeatStatus found
        """
        heartbeats = []
        for path in self.heartbeat_dir.iterdir():
            if path.is_file() and not path.name.startswith("."):
                try:
                    # Parse filename: {job_id}-{run_id}
                    parts = path.name.rsplit("-", 1)
                    if len(parts) == 2:
                        job_id, run_id_str = parts
                        run_id = int(run_id_str)
                        status = self.check_heartbeat(job_id, run_id)
                        if status:
                            heartbeats.append(status)
                except (ValueError, IndexError):
                    logger.warning(f"Invalid heartbeat filename: {path.name}")
        return heartbeats
    
    def cleanup_stale(self, max_age_hours: int = 24) -> int:
        """Clean up stale markers and heartbeats.
        
        Args:
            max_age_hours: Remove files older than this many hours
            
        Returns:
            Number of files removed
        """
        removed = 0
        max_age_seconds = max_age_hours * 3600
        now = time.time()
        
        for dir_path in [self.marker_dir, self.heartbeat_dir]:
            for path in dir_path.iterdir():
                if path.is_file() and path.name != "procclaw-wrapper.sh":
                    try:
                        age = now - path.stat().st_mtime
                        if age > max_age_seconds:
                            path.unlink()
                            removed += 1
                    except OSError:
                        pass
        
        if removed:
            logger.info(f"Cleaned up {removed} stale marker/heartbeat files")
        return removed


# Module-level instance for convenience
_manager: JobWrapperManager | None = None


def get_wrapper_manager() -> JobWrapperManager:
    """Get the singleton wrapper manager instance."""
    global _manager
    if _manager is None:
        _manager = JobWrapperManager()
    return _manager


def init_wrapper_manager(
    marker_dir: Path | None = None,
    heartbeat_dir: Path | None = None,
    **kwargs,
) -> JobWrapperManager:
    """Initialize the wrapper manager with custom settings."""
    global _manager
    _manager = JobWrapperManager(
        marker_dir=marker_dir,
        heartbeat_dir=heartbeat_dir,
        **kwargs,
    )
    return _manager
