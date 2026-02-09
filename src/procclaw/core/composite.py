"""Composite job execution (chain, group, chord)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Callable

from loguru import logger

if TYPE_CHECKING:
    from procclaw.core.supervisor import Supervisor


class CompositeStatus(str, Enum):
    """Status of a composite job execution."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class CompositeRun:
    """Tracks a running composite job."""
    composite_id: str
    type: str  # chain, group, chord
    jobs: list[str]
    callback: str | None = None
    status: CompositeStatus = CompositeStatus.PENDING
    current_step: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    results: dict[str, int] = field(default_factory=dict)  # job_id -> exit_code
    trigger: str = "manual"


class CompositeExecutor:
    """Executes composite jobs (chain, group, chord)."""
    
    def __init__(self, supervisor: Supervisor):
        self.supervisor = supervisor
        self.runs: dict[str, CompositeRun] = {}
        self._tasks: dict[str, asyncio.Task] = {}
    
    async def run_chain(self, composite_id: str, job_ids: list[str], trigger: str = "manual") -> CompositeRun:
        """Execute jobs sequentially (A → B → C). Stops on first failure."""
        run = CompositeRun(
            composite_id=composite_id,
            type="chain",
            jobs=job_ids,
            status=CompositeStatus.RUNNING,
            started_at=datetime.now(),
            trigger=trigger,
        )
        self.runs[composite_id] = run
        
        logger.info(f"Starting chain '{composite_id}': {' → '.join(job_ids)}")
        
        for i, job_id in enumerate(job_ids):
            run.current_step = i
            logger.info(f"Chain '{composite_id}' step {i+1}/{len(job_ids)}: {job_id}")
            
            # Start job and wait for completion
            exit_code = await self._run_and_wait(job_id, trigger, composite_id)
            run.results[job_id] = exit_code
            
            if exit_code != 0:
                logger.error(f"Chain '{composite_id}' failed at step {i+1} ({job_id}), exit code: {exit_code}")
                run.status = CompositeStatus.FAILED
                run.finished_at = datetime.now()
                return run
        
        logger.info(f"Chain '{composite_id}' completed successfully")
        run.status = CompositeStatus.COMPLETED
        run.finished_at = datetime.now()
        return run
    
    async def run_group(self, composite_id: str, job_ids: list[str], trigger: str = "manual") -> CompositeRun:
        """Execute jobs in parallel (A + B + C). Waits for all to complete."""
        run = CompositeRun(
            composite_id=composite_id,
            type="group",
            jobs=job_ids,
            status=CompositeStatus.RUNNING,
            started_at=datetime.now(),
            trigger=trigger,
        )
        self.runs[composite_id] = run
        
        logger.info(f"Starting group '{composite_id}': {' + '.join(job_ids)}")
        
        # Start all jobs in parallel
        tasks = [self._run_and_wait(job_id, trigger, composite_id) for job_id in job_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Collect results
        all_success = True
        for job_id, result in zip(job_ids, results):
            if isinstance(result, Exception):
                run.results[job_id] = -1
                all_success = False
                logger.error(f"Group '{composite_id}' job '{job_id}' failed with exception: {result}")
            else:
                run.results[job_id] = result
                if result != 0:
                    all_success = False
        
        run.status = CompositeStatus.COMPLETED if all_success else CompositeStatus.FAILED
        run.finished_at = datetime.now()
        logger.info(f"Group '{composite_id}' {'completed' if all_success else 'finished with failures'}")
        return run
    
    async def run_chord(self, composite_id: str, job_ids: list[str], callback: str, trigger: str = "manual") -> CompositeRun:
        """Execute jobs in parallel, then run callback when all complete."""
        run = CompositeRun(
            composite_id=composite_id,
            type="chord",
            jobs=job_ids,
            callback=callback,
            status=CompositeStatus.RUNNING,
            started_at=datetime.now(),
            trigger=trigger,
        )
        self.runs[composite_id] = run
        
        logger.info(f"Starting chord '{composite_id}': ({' + '.join(job_ids)}) → {callback}")
        
        # First, run the group
        tasks = [self._run_and_wait(job_id, trigger, composite_id) for job_id in job_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Collect results
        all_success = True
        for job_id, result in zip(job_ids, results):
            if isinstance(result, Exception):
                run.results[job_id] = -1
                all_success = False
            else:
                run.results[job_id] = result
                if result != 0:
                    all_success = False
        
        if not all_success:
            logger.warning(f"Chord '{composite_id}' group had failures, running callback anyway")
        
        # Run callback
        logger.info(f"Chord '{composite_id}' running callback: {callback}")
        callback_exit = await self._run_and_wait(callback, trigger, composite_id)
        run.results[callback] = callback_exit
        
        run.status = CompositeStatus.COMPLETED if (all_success and callback_exit == 0) else CompositeStatus.FAILED
        run.finished_at = datetime.now()
        logger.info(f"Chord '{composite_id}' {'completed' if run.status == CompositeStatus.COMPLETED else 'finished with failures'}")
        return run
    
    async def _run_and_wait(self, job_id: str, trigger: str, composite_id: str | None = None) -> int:
        """Start a job and wait for it to complete. Returns exit code."""
        job = self.supervisor.jobs.get_job(job_id)
        if not job:
            logger.error(f"Job '{job_id}' not found")
            return -1
        
        # Start the job
        self.supervisor.start_job(job_id, trigger=trigger, composite_id=composite_id)
        
        # Wait for completion by polling state
        while True:
            await asyncio.sleep(0.5)
            state = self.supervisor.db.get_state(job_id)
            if not state:
                return -1
            
            # Check if job is still running
            if state.pid and self.supervisor.check_pid(state.pid):
                continue
            
            # Job finished - check exit code
            return state.last_exit_code if state.last_exit_code is not None else -1
    
    def get_run(self, composite_id: str) -> CompositeRun | None:
        """Get a composite run by ID."""
        return self.runs.get(composite_id)
    
    def list_runs(self) -> list[CompositeRun]:
        """List all composite runs."""
        return list(self.runs.values())
