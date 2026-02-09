"""Resource monitoring for ProcClaw.

Monitors CPU, memory, and runtime for jobs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, TYPE_CHECKING

import psutil
from loguru import logger

if TYPE_CHECKING:
    from procclaw.models import JobConfig, ResourceLimitsConfig, ResourceLimitAction


@dataclass
class ResourceUsage:
    """Current resource usage for a job."""
    
    pid: int
    cpu_percent: float
    memory_mb: float
    runtime_seconds: float
    
    def __repr__(self) -> str:
        return (
            f"ResourceUsage(cpu={self.cpu_percent:.1f}%, "
            f"mem={self.memory_mb:.1f}MB, "
            f"runtime={self.runtime_seconds:.0f}s)"
        )


@dataclass
class ResourceViolation:
    """A resource limit violation."""
    
    job_id: str
    resource: str  # "cpu", "memory", "timeout"
    current_value: float
    limit_value: float
    action: "ResourceLimitAction"
    
    def __repr__(self) -> str:
        return (
            f"ResourceViolation({self.job_id}: {self.resource} "
            f"{self.current_value:.1f} > {self.limit_value:.1f})"
        )


class ResourceMonitor:
    """Monitors resource usage for running jobs.
    
    Periodically checks CPU, memory, and runtime against configured limits.
    Can warn, throttle, or kill jobs that exceed limits.
    """
    
    def __init__(
        self,
        on_violation: Callable[[ResourceViolation], None] | None = None,
        on_kill: Callable[[str, str], None] | None = None,
        default_check_interval: int = 30,
    ):
        """Initialize the resource monitor.
        
        Args:
            on_violation: Callback when a limit is violated
            on_kill: Callback when a job is killed (job_id, reason)
            default_check_interval: Default check interval in seconds
        """
        self._on_violation = on_violation
        self._on_kill = on_kill
        self._default_interval = default_check_interval
        
        # Tracked jobs: job_id -> (pid, config, started_at)
        self._jobs: dict[str, tuple[int, "ResourceLimitsConfig", datetime]] = {}
        
        # CPU samples for averaging
        self._cpu_samples: dict[str, list[float]] = {}
        
        self._running = False
    
    def register_job(
        self,
        job_id: str,
        pid: int,
        config: "ResourceLimitsConfig",
        started_at: datetime,
    ) -> None:
        """Register a job for resource monitoring.
        
        Args:
            job_id: The job ID
            pid: Process ID
            config: Resource limits configuration
            started_at: When the job started
        """
        if not config.enabled:
            return
        
        self._jobs[job_id] = (pid, config, started_at)
        self._cpu_samples[job_id] = []
        logger.debug(f"Registered job '{job_id}' for resource monitoring")
    
    def unregister_job(self, job_id: str) -> None:
        """Unregister a job from resource monitoring."""
        self._jobs.pop(job_id, None)
        self._cpu_samples.pop(job_id, None)
    
    def get_usage(self, job_id: str) -> ResourceUsage | None:
        """Get current resource usage for a job.
        
        Args:
            job_id: The job ID
            
        Returns:
            ResourceUsage or None if job not found/running
        """
        if job_id not in self._jobs:
            return None
        
        pid, config, started_at = self._jobs[job_id]
        
        try:
            proc = psutil.Process(pid)
            
            # Get CPU (this call measures over a short interval)
            cpu = proc.cpu_percent()
            
            # Get memory
            mem_info = proc.memory_info()
            mem_mb = mem_info.rss / (1024 * 1024)
            
            # Get runtime
            runtime = (datetime.now() - started_at).total_seconds()
            
            return ResourceUsage(
                pid=pid,
                cpu_percent=cpu,
                memory_mb=mem_mb,
                runtime_seconds=runtime,
            )
            
        except psutil.NoSuchProcess:
            logger.debug(f"Process for '{job_id}' no longer exists")
            self.unregister_job(job_id)
            return None
        except Exception as e:
            logger.warning(f"Failed to get resource usage for '{job_id}': {e}")
            return None
    
    def check_limits(self, job_id: str) -> list[ResourceViolation]:
        """Check if a job is within its resource limits.
        
        Args:
            job_id: The job ID
            
        Returns:
            List of violations found
        """
        if job_id not in self._jobs:
            return []
        
        pid, config, started_at = self._jobs[job_id]
        usage = self.get_usage(job_id)
        
        if usage is None:
            return []
        
        violations = []
        
        # Check CPU
        if config.max_cpu_percent is not None:
            # Add to samples for averaging
            self._cpu_samples[job_id].append(usage.cpu_percent)
            # Keep last 5 samples
            if len(self._cpu_samples[job_id]) > 5:
                self._cpu_samples[job_id].pop(0)
            
            avg_cpu = sum(self._cpu_samples[job_id]) / len(self._cpu_samples[job_id])
            
            if avg_cpu > config.max_cpu_percent:
                violations.append(ResourceViolation(
                    job_id=job_id,
                    resource="cpu",
                    current_value=avg_cpu,
                    limit_value=config.max_cpu_percent,
                    action=config.action,
                ))
        
        # Check memory
        if config.max_memory_mb is not None:
            if usage.memory_mb > config.max_memory_mb:
                violations.append(ResourceViolation(
                    job_id=job_id,
                    resource="memory",
                    current_value=usage.memory_mb,
                    limit_value=config.max_memory_mb,
                    action=config.action,
                ))
        
        # Check timeout
        if config.timeout_seconds is not None:
            if usage.runtime_seconds > config.timeout_seconds:
                violations.append(ResourceViolation(
                    job_id=job_id,
                    resource="timeout",
                    current_value=usage.runtime_seconds,
                    limit_value=config.timeout_seconds,
                    action=config.action,
                ))
        
        return violations
    
    def _handle_violation(self, violation: ResourceViolation) -> None:
        """Handle a resource limit violation.
        
        Args:
            violation: The violation to handle
        """
        from procclaw.models import ResourceLimitAction
        
        logger.warning(
            f"Resource limit exceeded for '{violation.job_id}': "
            f"{violation.resource} = {violation.current_value:.1f} > {violation.limit_value:.1f}"
        )
        
        if self._on_violation:
            self._on_violation(violation)
        
        if violation.action == ResourceLimitAction.KILL:
            self._kill_job(violation.job_id, violation)
        elif violation.action == ResourceLimitAction.THROTTLE:
            self._throttle_job(violation.job_id)
        # WARN just logs, which we already did
    
    def _kill_job(self, job_id: str, violation: ResourceViolation) -> None:
        """Kill a job due to resource violation."""
        if job_id not in self._jobs:
            return
        
        pid, _, _ = self._jobs[job_id]
        
        try:
            proc = psutil.Process(pid)
            reason = (
                f"{violation.resource} limit exceeded: "
                f"{violation.current_value:.1f} > {violation.limit_value:.1f}"
            )
            
            logger.warning(f"Killing job '{job_id}': {reason}")
            proc.kill()
            
            if self._on_kill:
                self._on_kill(job_id, reason)
                
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            logger.error(f"Failed to kill job '{job_id}': {e}")
    
    def _throttle_job(self, job_id: str) -> None:
        """Attempt to throttle a job's CPU usage."""
        if job_id not in self._jobs:
            return
        
        pid, _, _ = self._jobs[job_id]
        
        try:
            proc = psutil.Process(pid)
            # Set to lowest priority (niceness = 19 on Unix)
            proc.nice(19)
            logger.info(f"Throttled job '{job_id}' to low priority")
        except Exception as e:
            logger.warning(f"Failed to throttle job '{job_id}': {e}")
    
    async def run(self) -> None:
        """Run the resource monitor loop."""
        self._running = True
        logger.info(f"Resource monitor started")
        
        while self._running:
            try:
                await self._check_all()
            except Exception as e:
                logger.error(f"Resource monitor error: {e}")
            
            await asyncio.sleep(self._default_interval)
        
        logger.info("Resource monitor stopped")
    
    async def _check_all(self) -> None:
        """Check all registered jobs."""
        for job_id in list(self._jobs.keys()):
            violations = self.check_limits(job_id)
            
            for violation in violations:
                self._handle_violation(violation)
    
    def stop(self) -> None:
        """Stop the resource monitor."""
        self._running = False
    
    @property
    def is_running(self) -> bool:
        """Check if monitor is running."""
        return self._running
    
    @property
    def monitored_count(self) -> int:
        """Number of jobs being monitored."""
        return len(self._jobs)
