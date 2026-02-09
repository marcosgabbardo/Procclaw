"""Log rotation utilities for ProcClaw."""

from __future__ import annotations

import gzip
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from procclaw.models import LogConfig


def parse_size(size_str: str) -> int:
    """Parse a human-readable size string to bytes.
    
    Examples:
        "10MB" -> 10485760
        "1GB" -> 1073741824
        "500KB" -> 512000
        "1024" -> 1024
    
    Args:
        size_str: Size string like "10MB", "1GB", "500KB"
        
    Returns:
        Size in bytes
    """
    size_str = size_str.strip().upper()
    
    # If it's just a number, return as-is
    if size_str.isdigit():
        return int(size_str)
    
    # Match number + unit
    match = re.match(r'^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?$', size_str)
    if not match:
        raise ValueError(f"Invalid size format: {size_str}")
    
    number = float(match.group(1))
    unit = match.group(2) or "B"
    
    multipliers = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 * 1024,
        "GB": 1024 * 1024 * 1024,
        "TB": 1024 * 1024 * 1024 * 1024,
    }
    
    return int(number * multipliers[unit])


def get_log_files(base_path: Path) -> list[tuple[Path, int]]:
    """Get all rotated log files for a base path.
    
    Returns list of (path, index) tuples, sorted by index.
    Example: log.1, log.2, log.3 -> [(log.1, 1), (log.2, 2), (log.3, 3)]
    """
    parent = base_path.parent
    stem = base_path.stem
    suffix = base_path.suffix
    
    files = []
    
    # Find all rotated files
    for f in parent.glob(f"{stem}{suffix}.*"):
        # Match .1, .2, .gz, .1.gz, etc.
        name = f.name
        
        # Remove base name
        rest = name[len(stem) + len(suffix):]
        
        # Parse the index
        if rest.startswith("."):
            rest = rest[1:]
            
            # Handle .gz extension
            if rest.endswith(".gz"):
                rest = rest[:-3]
            
            if rest.isdigit():
                files.append((f, int(rest)))
    
    # Sort by index
    files.sort(key=lambda x: x[1])
    return files


def rotate_log(
    log_path: Path,
    max_files: int = 5,
    compress: bool = True,
) -> bool:
    """Rotate a log file.
    
    Renames log files in sequence:
    - log.log -> log.log.1
    - log.log.1 -> log.log.2
    - etc.
    
    If compress=True, older files are gzipped.
    
    Args:
        log_path: Path to the log file
        max_files: Maximum number of rotated files to keep
        compress: Whether to gzip older files
        
    Returns:
        True if rotation was performed
    """
    if not log_path.exists():
        return False
    
    # Get existing rotated files
    rotated = get_log_files(log_path)
    
    # Remove oldest files if we have too many
    while len(rotated) >= max_files:
        oldest_path, _ = rotated[-1]
        logger.debug(f"Removing old log: {oldest_path}")
        oldest_path.unlink()
        rotated.pop()
    
    # Rotate existing files (work backwards to avoid overwriting)
    for path, idx in reversed(rotated):
        new_idx = idx + 1
        
        # Determine new path
        if path.suffix == ".gz":
            # Already compressed
            new_path = log_path.with_suffix(f"{log_path.suffix}.{new_idx}.gz")
        elif new_idx > 1 and compress:
            # Compress older files
            new_path = log_path.with_suffix(f"{log_path.suffix}.{new_idx}.gz")
            _compress_file(path, new_path)
            path.unlink()
            continue
        else:
            new_path = log_path.with_suffix(f"{log_path.suffix}.{new_idx}")
        
        logger.debug(f"Rotating: {path} -> {new_path}")
        path.rename(new_path)
    
    # Rotate current log to .1
    new_path = log_path.with_suffix(f"{log_path.suffix}.1")
    logger.debug(f"Rotating current: {log_path} -> {new_path}")
    log_path.rename(new_path)
    
    return True


def _compress_file(src: Path, dst: Path) -> None:
    """Compress a file with gzip."""
    with open(src, "rb") as f_in:
        with gzip.open(dst, "wb") as f_out:
            f_out.writelines(f_in)


def check_and_rotate(
    log_path: Path,
    config: "LogConfig",
) -> bool:
    """Check if a log file needs rotation and rotate if necessary.
    
    Args:
        log_path: Path to the log file
        config: Log configuration
        
    Returns:
        True if rotation was performed
    """
    if not log_path.exists():
        return False
    
    max_bytes = parse_size(config.max_size)
    current_size = log_path.stat().st_size
    
    if current_size >= max_bytes:
        logger.info(
            f"Log rotation triggered for {log_path.name} "
            f"({current_size / 1024 / 1024:.1f}MB >= {config.max_size})"
        )
        return rotate_log(log_path, max_files=config.rotate, compress=True)
    
    return False


def cleanup_old_logs(
    logs_dir: Path,
    max_age_days: int = 7,
) -> int:
    """Clean up old log files.
    
    Removes rotated log files older than max_age_days.
    
    Args:
        logs_dir: Directory containing log files
        max_age_days: Maximum age in days
        
    Returns:
        Number of files removed
    """
    if not logs_dir.exists():
        return 0
    
    cutoff = datetime.now() - timedelta(days=max_age_days)
    removed = 0
    
    for log_file in logs_dir.glob("*.[0-9]*"):
        try:
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
            if mtime < cutoff:
                logger.debug(f"Removing old log: {log_file}")
                log_file.unlink()
                removed += 1
        except Exception as e:
            logger.warning(f"Failed to check/remove {log_file}: {e}")
    
    for log_file in logs_dir.glob("*.gz"):
        try:
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
            if mtime < cutoff:
                logger.debug(f"Removing old log: {log_file}")
                log_file.unlink()
                removed += 1
        except Exception as e:
            logger.warning(f"Failed to check/remove {log_file}: {e}")
    
    if removed > 0:
        logger.info(f"Cleaned up {removed} old log files")
    
    return removed


class LogRotator:
    """Manages log rotation for all jobs.
    
    Periodically checks log files and rotates them if they exceed
    the configured size limit.
    """
    
    def __init__(
        self,
        logs_dir: Path,
        check_interval: int = 300,  # 5 minutes
        max_age_days: int = 7,
    ):
        """Initialize the log rotator.
        
        Args:
            logs_dir: Directory containing log files
            check_interval: How often to check for rotation (seconds)
            max_age_days: Maximum age for old log files
        """
        self._logs_dir = logs_dir
        self._check_interval = check_interval
        self._max_age_days = max_age_days
        self._running = False
        self._jobs: dict[str, "LogConfig"] = {}
    
    def register_job(self, job_id: str, config: "LogConfig") -> None:
        """Register a job's log configuration."""
        self._jobs[job_id] = config
    
    def unregister_job(self, job_id: str) -> None:
        """Unregister a job."""
        self._jobs.pop(job_id, None)
    
    def check_all(self) -> int:
        """Check and rotate all job logs.
        
        Returns:
            Number of logs rotated
        """
        rotated = 0
        
        for job_id, config in self._jobs.items():
            stdout_path = self._logs_dir / f"{job_id}.log"
            stderr_path = self._logs_dir / f"{job_id}.error.log"
            
            if check_and_rotate(stdout_path, config):
                rotated += 1
            if check_and_rotate(stderr_path, config):
                rotated += 1
        
        return rotated
    
    async def run(self) -> None:
        """Run the log rotation loop."""
        import asyncio
        
        self._running = True
        logger.info(f"Log rotator started (check interval: {self._check_interval}s)")
        
        while self._running:
            try:
                # Check for rotation
                rotated = self.check_all()
                if rotated:
                    logger.info(f"Rotated {rotated} log files")
                
                # Clean up old logs
                cleanup_old_logs(self._logs_dir, self._max_age_days)
                
            except Exception as e:
                logger.error(f"Log rotator error: {e}")
            
            await asyncio.sleep(self._check_interval)
        
        logger.info("Log rotator stopped")
    
    def stop(self) -> None:
        """Stop the log rotator."""
        self._running = False
    
    @property
    def is_running(self) -> bool:
        """Check if rotator is running."""
        return self._running
